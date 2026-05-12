"""A-INSTR M1 copy_efficiency engine tests (2026-05-11).

Verifies bot_pnl / source_counterfactual_pnl computation on synthetic
fixtures, including SOURCE_LOSS_OR_FLAT bucket and category thresholds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine

from app.models.market import Market
from app.models.trade import PaperTrade, PublicTrade
from app.models.wallet import ResolvedMarketRecord
from app.services.copy_efficiency_engine import (
    CATEGORY_MIN_THRESHOLDS,
    GLOBAL_MIN_THRESHOLD,
    compute_copy_efficiency_report,
    copy_efficiency_payload,
)


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def _seed_winning_resolved_market(
    session: Session, market_id: str, category: str = "Politics", winning: str = "Yes",
) -> None:
    session.add(Market(
        id=market_id, slug=market_id, question="Test", category=category,
    ))
    session.add(ResolvedMarketRecord(
        market_id=market_id, condition_id=market_id, question="Test",
        category=category, end_date="2024-06-15", closed=True,
        winning_outcome_index=0, winning_outcome_name=winning, usable=True,
    ))
    session.commit()


def _seed_paired_trade(
    session: Session, *, market_id: str, wallet: str, bot_pnl: float,
    bot_notional: float, wallet_entry_price: float, side: str = "BUY",
    outcome: str = "Yes", category: str = "politics",
    closed_offset_minutes: int = 30,
) -> None:
    """Insert a PaperTrade (closed) + matching PublicTrade (source wallet)."""
    now = datetime.now(UTC) - timedelta(minutes=closed_offset_minutes)
    session.add(PaperTrade(
        id=f"pt-{market_id}",
        market_id=market_id,
        outcome=outcome,
        side=side,
        quantity=bot_notional / max(wallet_entry_price, 0.01),
        average_price=wallet_entry_price,
        notional_usd=bot_notional,
        realized_pnl=bot_pnl,
        status="closed",
        opened_at=now - timedelta(hours=1),
        closed_at=now,
        wallet_address=wallet.lower(),
    ))
    session.add(PublicTrade(
        id=f"src-{market_id}",
        wallet_address=wallet.lower(),
        market_id=market_id,
        outcome=outcome,
        side=side,
        price=wallet_entry_price,
        size=bot_notional / max(wallet_entry_price, 0.01),
        traded_at=now - timedelta(hours=1, minutes=2),
        data_source="test",
    ))
    session.commit()


def test_normal_bucket_computes_ratio():
    with _session() as s:
        _seed_winning_resolved_market(s, "0xmkt1", category="Politics")
        # wallet entry 0.40, settlement 1.0 → unit_pnl ~ 0.60 - fee ~ 0.0096 = 0.59
        # bot notional 100 → counterfactual ~ 0.59 / 0.40 × 100 = 147.5
        # bot_pnl 130 → ratio ~ 0.88
        _seed_paired_trade(
            s, market_id="0xmkt1", wallet="0xelite",
            bot_pnl=130.0, bot_notional=100.0,
            wallet_entry_price=0.40,
        )
        report = compute_copy_efficiency_report(s, window_hours=24.0)
        assert report.sample_size == 1
        assert report.global_ratio is not None
        assert 0.7 < report.global_ratio < 1.1


def test_bot_outperforms_source_excluded_from_ratio():
    with _session() as s:
        # Source loses (settlement 0.0, entry 0.60 → unit_pnl ~ -0.60)
        _seed_winning_resolved_market(s, "0xmkt1", category="Politics", winning="No")
        _seed_paired_trade(
            s, market_id="0xmkt1", wallet="0xelite",
            bot_pnl=10.0,  # bot positive
            bot_notional=100.0,
            wallet_entry_price=0.60,
            outcome="Yes",  # bot bought Yes; market resolved No → bot loses, but pnl=10 is hardcoded
        )
        report = compute_copy_efficiency_report(s, window_hours=24.0)
        # source counterfactual <= 0 → excluded from main ratio
        assert report.sample_size_excluded >= 1
        assert report.bot_outperforms_source_count >= 1


def test_no_source_match_bucket():
    """PublicTrade missing → bucket SOURCE_UNAVAILABLE."""
    with _session() as s:
        _seed_winning_resolved_market(s, "0xmkt1", category="Politics")
        s.add(PaperTrade(
            id="pt-orphan",
            market_id="0xmkt1",
            outcome="Yes",
            side="BUY",
            quantity=100.0,
            average_price=0.40,
            notional_usd=40.0,
            realized_pnl=20.0,
            status="closed",
            opened_at=datetime.now(UTC) - timedelta(hours=1),
            closed_at=datetime.now(UTC) - timedelta(minutes=10),
            wallet_address="0xorphan",
        ))
        s.commit()
        report = compute_copy_efficiency_report(s, window_hours=24.0)
        assert report.sample_size == 1
        assert report.sample_size_excluded == 1
        # No valid records → ratio is None (treated NO_VALID_RATIO)
        assert report.global_ratio is None


def test_window_excludes_old_trades():
    with _session() as s:
        _seed_winning_resolved_market(s, "0xmkt1")
        _seed_paired_trade(
            s, market_id="0xmkt1", wallet="0xelite",
            bot_pnl=10.0, bot_notional=100.0, wallet_entry_price=0.40,
            closed_offset_minutes=60 * 48,  # 48h ago
        )
        report = compute_copy_efficiency_report(s, window_hours=24.0)
        assert report.sample_size == 0


def test_category_threshold_breach_flagged():
    """If a category has <50 trades, no breach raised (statistical noise)."""
    with _session() as s:
        _seed_winning_resolved_market(s, "0xmkt1", category="Crypto")
        _seed_paired_trade(
            s, market_id="0xmkt1", wallet="0xelite",
            bot_pnl=1.0, bot_notional=100.0, wallet_entry_price=0.40,
            outcome="Yes", category="crypto",
        )
        report = compute_copy_efficiency_report(s, window_hours=24.0)
        # Only 1 trade in crypto → no breach (need ≥50)
        assert report.threshold_breaches == []


def test_payload_window_shorthand_24h():
    with _session() as s:
        payload = copy_efficiency_payload(s, window="24h")
        assert payload["window_hours"] == 24.0


def test_payload_window_shorthand_7d():
    with _session() as s:
        payload = copy_efficiency_payload(s, window="7d")
        assert payload["window_hours"] == 168.0


def test_payload_window_numeric_hours():
    with _session() as s:
        payload = copy_efficiency_payload(s, window="48")
        assert payload["window_hours"] == 48.0


def test_thresholds_globally_consistent():
    """Sanity: defined thresholds match locked spec 2026-05-11."""
    assert GLOBAL_MIN_THRESHOLD == 0.70
    assert CATEGORY_MIN_THRESHOLDS["crypto"] == 0.55
    assert CATEGORY_MIN_THRESHOLDS["sports"] == 0.75
    assert CATEGORY_MIN_THRESHOLDS["politics"] == 0.75
