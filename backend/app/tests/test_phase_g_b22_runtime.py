"""Phase G — wire B22 incremental in close-loop runtime (2026-05-13).

Verifies that _close_paper_with_reason() on CLOSED_RESOLVED triggers an
MFWR W+L increment (with audit_at dedup) for the source wallet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.bot import BotState
from app.models.trade import PaperTrade
from app.models.wallet import MarketFirstWalletRecord, ResolvedMarketRecord
from app.services.paper_trading_engine import (
    CLOSE_REASON_MANUAL,
    CLOSE_REASON_RESOLVED,
    _b22_runtime_update_mfwr_on_resolved_close,
    _close_paper_with_reason,
)


UTC = timezone.utc


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(BotState(id=1, paper_capital=100.0))
        s.commit()
        yield s


def _make_trade(session, wallet, market, outcome, side="BUY"):
    tr = PaperTrade(
        id=f"pt-{market}-{wallet}",
        market_id=market,
        outcome=outcome,
        side=side,
        quantity=10.0,
        average_price=0.50,
        notional_usd=5.0,
        status="open",
        opened_at=datetime.now(UTC) - timedelta(minutes=10),
        wallet_address=wallet,
    )
    session.add(tr)
    session.commit()
    session.refresh(tr)
    return tr


def _make_mfwr(session, wallet, wins=50, losses=2, audit_at=None):
    mfwr = MarketFirstWalletRecord(
        address=wallet,
        candidate_status="STRONG",
        resolved_winning_markets=wins,
        resolved_losing_markets=losses,
        resolved_market_win_rate=wins / (wins + losses) if (wins + losses) else 0.0,
        audit_at=audit_at or (datetime.now(UTC) - timedelta(days=10)),
        composite_score=80.0,
    )
    session.add(mfwr)
    session.commit()
    return mfwr


def _make_rmr(session, market_id, winning, end_date=None, condition_id=None):
    rmr = ResolvedMarketRecord(
        market_id=market_id,
        condition_id=condition_id or market_id,
        question="Test market",
        end_date=(end_date or datetime.now(UTC)).isoformat(),
        closed=True,
        winning_outcome_index=0,
        winning_outcome_name=winning,
        usable=True,
    )
    session.add(rmr)
    session.commit()
    return rmr


# ---------------- Direct helper tests ----------------


def test_b22_runtime_increments_win_when_outcome_matches(session):
    """Bot bet Yes, market resolved Yes → wallet gets +1 win."""
    _make_mfwr(session, "0xelite", wins=50, losses=2)
    _make_rmr(session, "0xmkt", winning="Yes")
    trade = _make_trade(session, "0xelite", "0xmkt", "Yes")
    _b22_runtime_update_mfwr_on_resolved_close(session, trade)
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xelite")
    ).one()
    assert mfwr.resolved_winning_markets == 51
    assert mfwr.resolved_losing_markets == 2
    assert abs(mfwr.resolved_market_win_rate - 51/53) < 1e-6


def test_b22_runtime_increments_loss_when_outcome_mismatch(session):
    """Bot bet Yes, market resolved No → wallet gets +1 loss."""
    _make_mfwr(session, "0xwallet", wins=80, losses=5)
    _make_rmr(session, "0xmkt2", winning="No")
    trade = _make_trade(session, "0xwallet", "0xmkt2", "Yes")
    _b22_runtime_update_mfwr_on_resolved_close(session, trade)
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xwallet")
    ).one()
    assert mfwr.resolved_winning_markets == 80
    assert mfwr.resolved_losing_markets == 6


def test_b22_runtime_no_longer_dedup_via_audit_at(session):
    """OBSOLETE → updated: with ledger in place, dedup is via ledger
    uniqueness, NOT audit_at. A market resolved before audit_at is now
    counted (first occurrence) — ledger ensures it won't be re-counted."""
    audit_at = datetime.now(UTC)
    _make_mfwr(session, "0xskip", wins=100, losses=5, audit_at=audit_at)
    # Market resolved BEFORE audit_at — under old dedup-by-audit_at logic,
    # this would have been skipped. Under new ledger logic, the FIRST call
    # counts it (no prior ledger row). Idempotence kicks in on SECOND call.
    _make_rmr(session, "0xold", winning="Yes",
              end_date=audit_at - timedelta(days=1))
    trade = _make_trade(session, "0xskip", "0xold", "Yes")
    _b22_runtime_update_mfwr_on_resolved_close(session, trade)
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xskip")
    ).one()
    # New logic: counted on first call (ledger row inserted)
    assert mfwr.resolved_winning_markets == 101
    assert mfwr.resolved_losing_markets == 5


def test_b22_runtime_finds_rmr_via_condition_id(session):
    """RMR has different PK market_id than the hex used in PaperTrade."""
    _make_mfwr(session, "0xfound", wins=30, losses=1)
    _make_rmr(
        session,
        market_id="numeric-pk",  # different from PaperTrade.market_id
        condition_id="0xhexcondition",
        winning="Yes",
    )
    trade = _make_trade(session, "0xfound", "0xhexcondition", "Yes")
    _b22_runtime_update_mfwr_on_resolved_close(session, trade)
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xfound")
    ).one()
    assert mfwr.resolved_winning_markets == 31


def test_b22_runtime_no_mfwr_silent_skip(session):
    """If wallet not in MFWR, no error — silently skip."""
    _make_rmr(session, "0xmkt", winning="Yes")
    trade = _make_trade(session, "0xnomfwr", "0xmkt", "Yes")
    # Should not raise
    _b22_runtime_update_mfwr_on_resolved_close(session, trade)


def test_b22_runtime_no_rmr_silent_skip(session):
    """If market not resolved in DB, no error — silently skip."""
    _make_mfwr(session, "0xready", wins=30, losses=1)
    trade = _make_trade(session, "0xready", "0xnotresolved", "Yes")
    _b22_runtime_update_mfwr_on_resolved_close(session, trade)
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xready")
    ).one()
    assert mfwr.resolved_winning_markets == 30  # unchanged


# ---------------- Integration with close-paper-with-reason ----------------


def test_close_resolved_triggers_b22(session, monkeypatch):
    """CLOSED_RESOLVED close triggers _b22_runtime_update — ONLY when feature
    flag enable_b22_runtime_hook=True. Default is False per HOTFIX 2026-05-13.
    """
    # Enable the flag for this test
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_b22_runtime_hook", True)

    _make_mfwr(session, "0xint", wins=50, losses=2)
    _make_rmr(session, "0xintmkt", winning="Yes")
    trade = _make_trade(session, "0xint", "0xintmkt", "Yes")
    _close_paper_with_reason(session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=1.0)
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xint")
    ).one()
    assert mfwr.resolved_winning_markets == 51


def test_close_manual_does_not_trigger_b22(session):
    """CLOSE_REASON_MANUAL must NOT trigger MFWR refresh (manual exit ≠ resolution)."""
    _make_mfwr(session, "0xmanual", wins=50, losses=2)
    _make_rmr(session, "0xmanmkt", winning="Yes")
    trade = _make_trade(session, "0xmanual", "0xmanmkt", "Yes")
    _close_paper_with_reason(session, trade, reason=CLOSE_REASON_MANUAL, exit_price=0.60)
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xmanual")
    ).one()
    # Should NOT have changed
    assert mfwr.resolved_winning_markets == 50
    assert mfwr.resolved_losing_markets == 2


def test_b22_runtime_failure_does_not_crash_close(session, monkeypatch):
    """If B22 helper raises, the trade close must still succeed."""
    _make_rmr(session, "0xcrashmkt", winning="Yes")
    trade = _make_trade(session, "0xcrash", "0xcrashmkt", "Yes")

    # Make the MFWR query raise
    def _explode(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    import app.services.paper_trading_engine as pte
    monkeypatch.setattr(pte, "_b22_runtime_update_mfwr_on_resolved_close", _explode)
    try:
        _close_paper_with_reason(session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=1.0)
    except RuntimeError:
        pass  # expected — but trade should be committed already
    refreshed = session.get(PaperTrade, trade.id)
    assert refreshed.status == "closed"  # already committed before B22 hook


# ============================================================
# Phase G HOTFIX (Round 8 review audit 2026-05-13) — tests qui catch
# le vrai bug : runtime processes ONE market, mais audit_at = now() rendrait
# invisibles les autres markets non comptés. Test critique.
# ============================================================


def test_HOTFIX_runtime_must_not_invisibilize_uncounted_markets(session):
    """
    BUG INITIAL (review audit 2026-05-13) :
    Le helper runtime set `audit_at = now()` après processing UN seul market.
    Conséquence : tous les autres markets résolus entre l'ancien audit_at
    et now mais NON encore comptés deviennent invisibles pour le batch B22
    (filtre rmr.end_date > audit_at les exclut).

    Ce test simule le scénario :
      - wallet audit_at = 2026-04-29
      - 3 markets résolus entre 2026-05-01 et 2026-05-13
      - runtime ne traite QUE le market #3 (le plus récent)
      - les markets #1 et #2 doivent rester comptables par le batch futur

    Avec le bug : audit_at devient now() = 2026-05-13, donc end_date des
    markets #1 et #2 (< now) deviennent <= audit_at → ils sont skip.
    """
    from datetime import datetime as _dt, timedelta as _td
    old_audit = _dt(2026, 4, 29, tzinfo=UTC)
    _make_mfwr(session, "0xbacklog", wins=80, losses=2, audit_at=old_audit)

    # 3 markets résolus à des dates différentes, tous APRÈS old audit_at
    _make_rmr(
        session, market_id="0xmkt-may01", winning="Yes",
        end_date=_dt(2026, 5, 1, tzinfo=UTC),
    )
    _make_rmr(
        session, market_id="0xmkt-may07", winning="Yes",
        end_date=_dt(2026, 5, 7, tzinfo=UTC),
    )
    _make_rmr(
        session, market_id="0xmkt-may13", winning="Yes",
        end_date=_dt(2026, 5, 13, tzinfo=UTC),
    )

    # Runtime traite UNIQUEMENT le market #3 (le plus récent)
    trade = _make_trade(session, "0xbacklog", "0xmkt-may13", "Yes")
    _b22_runtime_update_mfwr_on_resolved_close(session, trade)

    # Après le runtime, audit_at NE DOIT PAS être avancé à now() — sinon
    # les 2 autres markets seront skip par un batch futur.
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xbacklog")
    ).one()
    audit_at_after = mfwr.audit_at
    if audit_at_after and audit_at_after.tzinfo is None:
        audit_at_after = audit_at_after.replace(tzinfo=UTC)

    # Le batch utilise `rmr.end_date > audit_at` pour décider de comptablité.
    # Les markets #1 (may01) et #2 (may07) doivent rester comptables.
    may01 = _dt(2026, 5, 1, tzinfo=UTC)
    may07 = _dt(2026, 5, 7, tzinfo=UTC)
    assert audit_at_after is None or audit_at_after <= may01, (
        f"REGRESSION : audit_at={audit_at_after} > may01={may01} → market #1 "
        f"deviendra invisible pour le batch B22. C'est exactement le bug "
        f"identifié par review 2026-05-13. Le runtime ne doit pas avancer "
        f"audit_at après processing d'un seul market — il faut un ledger."
    )
    assert audit_at_after is None or audit_at_after <= may07, (
        f"REGRESSION : audit_at={audit_at_after} > may07={may07} → market #2 "
        f"deviendra invisible pour le batch B22."
    )


def test_LEDGER_hook_enabled_by_default(session):
    """With WalletMarketResolutionAudit ledger in place, the runtime hook is
    safe and can be enabled by default."""
    from app.config import get_settings
    s = get_settings()
    assert getattr(s, "enable_b22_runtime_hook", False) is True, (
        "After ledger implementation 2026-05-13, the hook should be enabled "
        "by default (ledger guarantees idempotence)."
    )


def test_HOTFIX_close_resolved_skips_b22_when_flag_off(session, monkeypatch):
    """Quand enable_b22_runtime_hook=False, le close RESOLVED ne touche
    PAS MFWR. Le batch B22 reste source of truth."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_b22_runtime_hook", False)
    _make_mfwr(session, "0xskip", wins=50, losses=2, audit_at=datetime.now(UTC) - timedelta(days=10))
    _make_rmr(session, "0xskipmkt", winning="Yes")
    trade = _make_trade(session, "0xskip", "0xskipmkt", "Yes")
    _close_paper_with_reason(session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=1.0)
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xskip")
    ).one()
    # MFWR doit être inchangé (hook désactivé)
    assert mfwr.resolved_winning_markets == 50, (
        "Le hook ne doit pas modifier MFWR quand enable_b22_runtime_hook=False"
    )


# ============================================================
# Phase G LEDGER tests (P0 implementation 2026-05-13)
# WalletMarketResolutionAudit unique(wallet, market, outcome).
# ============================================================


def test_LEDGER_runtime_writes_ledger_row(session, monkeypatch):
    """When B22 hook runs, a WalletMarketResolutionAudit row must be inserted."""
    from app.config import get_settings
    from app.models.trade import WalletMarketResolutionAudit
    monkeypatch.setattr(get_settings(), "enable_b22_runtime_hook", True)

    _make_mfwr(session, "0xledger", wins=50, losses=2)
    _make_rmr(session, "0xledgermkt", winning="Yes")
    trade = _make_trade(session, "0xledger", "0xledgermkt", "Yes")
    _close_paper_with_reason(session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=1.0)

    rows = session.exec(
        select(WalletMarketResolutionAudit).where(
            WalletMarketResolutionAudit.wallet_address == "0xledger"
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].market_id == "0xledgermkt"
    assert rows[0].is_win is True
    assert rows[0].source == "RUNTIME_CLOSE"


def test_LEDGER_idempotent_same_wallet_market_outcome(session, monkeypatch):
    """Calling the helper twice for the same (wallet, market, outcome) must
    increment MFWR only ONCE (via UniqueConstraint)."""
    from app.config import get_settings
    from app.models.trade import WalletMarketResolutionAudit
    monkeypatch.setattr(get_settings(), "enable_b22_runtime_hook", True)

    _make_mfwr(session, "0xidem", wins=50, losses=2)
    _make_rmr(session, "0xidemmkt", winning="Yes")
    trade1 = _make_trade(session, "0xidem", "0xidemmkt", "Yes")
    _close_paper_with_reason(session, trade1, reason=CLOSE_REASON_RESOLVED, exit_price=1.0)

    mfwr_after_first = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xidem")
    ).one()
    assert mfwr_after_first.resolved_winning_markets == 51  # first increment OK

    # Now simulate a SECOND close on the same (wallet, market, outcome)
    # (e.g. partial close + re-resolution scenario, or duplicate trade)
    trade2 = PaperTrade(
        id="pt-idem-2",
        market_id="0xidemmkt",
        outcome="Yes",
        side="BUY",
        quantity=10.0,
        average_price=0.50,
        notional_usd=5.0,
        status="open",
        opened_at=datetime.now(UTC) - timedelta(minutes=5),
        wallet_address="0xidem",
    )
    session.add(trade2)
    session.commit()
    _close_paper_with_reason(session, trade2, reason=CLOSE_REASON_RESOLVED, exit_price=1.0)

    mfwr_after_second = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xidem")
    ).one()
    # Must STILL be 51 — second call did NOT double-count
    assert mfwr_after_second.resolved_winning_markets == 51, (
        f"Ledger dedup failed — got {mfwr_after_second.resolved_winning_markets}, "
        f"expected 51 (only first close should count)"
    )
    # Only 1 ledger row total
    rows = session.exec(
        select(WalletMarketResolutionAudit).where(
            WalletMarketResolutionAudit.wallet_address == "0xidem"
        )
    ).all()
    assert len(rows) == 1


def test_LEDGER_counts_two_distinct_markets(session, monkeypatch):
    """Two different markets for the same wallet must BOTH be counted."""
    from app.config import get_settings
    from app.models.trade import WalletMarketResolutionAudit
    monkeypatch.setattr(get_settings(), "enable_b22_runtime_hook", True)

    _make_mfwr(session, "0xtwo", wins=50, losses=2)
    _make_rmr(session, "0xmkt-A", winning="Yes")
    _make_rmr(session, "0xmkt-B", winning="No")

    trade_a = _make_trade(session, "0xtwo", "0xmkt-A", "Yes")
    _close_paper_with_reason(session, trade_a, reason=CLOSE_REASON_RESOLVED, exit_price=1.0)

    trade_b = _make_trade(session, "0xtwo", "0xmkt-B", "Yes")
    _close_paper_with_reason(session, trade_b, reason=CLOSE_REASON_RESOLVED, exit_price=0.0)

    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xtwo")
    ).one()
    assert mfwr.resolved_winning_markets == 51  # +1 from mkt-A (Yes won)
    assert mfwr.resolved_losing_markets == 3    # +1 from mkt-B (Yes lost, winner was No)

    rows = session.exec(
        select(WalletMarketResolutionAudit).where(
            WalletMarketResolutionAudit.wallet_address == "0xtwo"
        )
    ).all()
    assert len(rows) == 2


def test_LEDGER_runtime_NEVER_touches_audit_at(session, monkeypatch):
    """The runtime helper must never advance MFWR.audit_at.
    (Replaces the dedup-by-audit_at logic that broke the batch view.)"""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_b22_runtime_hook", True)

    old_audit = datetime(2026, 4, 29, tzinfo=UTC)
    _make_mfwr(session, "0xkeepauditat", wins=50, losses=2, audit_at=old_audit)
    _make_rmr(session, "0xmktX", winning="Yes",
              end_date=datetime(2026, 5, 13, tzinfo=UTC))
    trade = _make_trade(session, "0xkeepauditat", "0xmktX", "Yes")
    _close_paper_with_reason(session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=1.0)

    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(MarketFirstWalletRecord.address == "0xkeepauditat")
    ).one()
    audit_at_after = mfwr.audit_at
    if audit_at_after and audit_at_after.tzinfo is None:
        audit_at_after = audit_at_after.replace(tzinfo=UTC)
    assert audit_at_after == old_audit, (
        f"Runtime must NOT change audit_at. Got {audit_at_after}, "
        f"expected {old_audit} (unchanged)."
    )
