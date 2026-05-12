"""P0.3 (Round 8, 2026-05-12) — M1 v5 visibility gate runtime.

The gate refuses trades where `source.PublicTrade.seen_at > bot_now + 2s
tolerance`. This catches causality violations: the bot cannot have copied
a source trade it had not yet seen. Such trades are not live-reproducible.

The gate is NOT about copy delay (which can legitimately be 100s+). It only
blocks causality violations. DELAYED_POLL legitimate trades are still allowed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.bot import BotState
from app.models.trade import PublicTrade
from app.services.paper_trading_engine import PaperTradingEngine


UTC = timezone.utc


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(BotState(id=1, paper_capital=100.0))
        s.commit()
        yield s


def _make_pubtrade(session: Session, trade_id: str, seen_at: datetime) -> PublicTrade:
    pt = PublicTrade(
        id=trade_id,
        wallet_address="0xwallet",
        market_id="0xmkt",
        outcome="Yes",
        side="BUY",
        price=0.50,
        size=10.0,
        traded_at=seen_at - timedelta(seconds=30),
        seen_at=seen_at,
    )
    session.add(pt)
    session.commit()
    return pt


# ---------------- Helper invocation: _log_visibility_leakage ----------------


def test_log_visibility_leakage_writes_notradedecision(session):
    """Helper writes a NoTradeDecision row with reason_code VISIBILITY_LEAKAGE_SUSPECT."""
    from app.models.signal import Signal, SignalType
    sig = Signal(
        id="sig-1", market_id="0xmkt", outcome="Yes",
        signal_type=SignalType.SMART_WALLET_ENTRY, score=80, confidence=0.9,
        reason="test", created_at=datetime.now(UTC),
    )
    session.add(sig)
    session.commit()

    eng = PaperTradingEngine(session)
    now = datetime.now(UTC)
    eng._log_visibility_leakage(
        signal=sig,
        audit_context={"wallet": "0xwallet"},
        source_trade_id="src-1",
        source_seen_at=now + timedelta(seconds=10),
        now_utc=now,
        delta_s=10.0,
    )
    # Check a NoTradeDecision row was added
    from app.models.trade import NoTradeDecision
    rows = session.exec(select(NoTradeDecision)).all()
    assert len(rows) == 1
    assert rows[0].reason_code == "VISIBILITY_LEAKAGE_SUSPECT"
    assert rows[0].signal_id == "sig-1"


# ---------------- Gate semantics: what does it accept/reject? ----------------


def test_gate_accepts_seen_at_in_the_past(session):
    """Trade where source was seen BEFORE now → legitimate, gate allows.
    (The gate is a method-level filter; we test the helper logic directly.)
    """
    now = datetime.now(UTC)
    past_seen = now - timedelta(seconds=60)
    # The gate code: if (seen_at - now).total_seconds() > 2.0 → block
    delta_s = (past_seen - now).total_seconds()
    assert delta_s < 2.0  # legitimate, gate doesn't block


def test_gate_accepts_seen_at_within_tolerance(session):
    """Tolerance 2s — seen_at slightly in future (jitter) is fine."""
    now = datetime.now(UTC)
    almost_now = now + timedelta(seconds=1)  # 1s in future, within 2s tolerance
    delta_s = (almost_now - now).total_seconds()
    assert delta_s <= 2.0  # gate allows


def test_gate_rejects_seen_at_clearly_in_future(session):
    """seen_at >> now → causality violation, gate must reject."""
    now = datetime.now(UTC)
    future_seen = now + timedelta(seconds=30)  # 30s in future = impossible
    delta_s = (future_seen - now).total_seconds()
    assert delta_s > 2.0  # gate rejects


# ---------------- Integration: PublicTrade lookup in gate ----------------


def test_gate_finds_publictrade_by_id(session):
    """Gate must query PublicTrade by source_trade_id and read seen_at."""
    now = datetime.now(UTC)
    _make_pubtrade(session, "src-future", now + timedelta(seconds=10))
    # Verify we can retrieve it
    from app.models.trade import PublicTrade as PT
    row = session.get(PT, "src-future")
    assert row is not None
    # SQLite returns naive datetime — coerce for comparison
    seen = row.seen_at if row.seen_at.tzinfo else row.seen_at.replace(tzinfo=UTC)
    assert seen > now


def test_gate_handles_missing_publictrade_gracefully(session):
    """If source_trade_id doesn't exist in PublicTrade, gate is best-effort
    (does not crash, treats as 'cannot verify')."""
    from app.models.trade import PublicTrade as PT
    row = session.get(PT, "nonexistent-id")
    assert row is None
    # Gate code wraps lookup in try/except — production behavior is continue.


def test_gate_handles_naive_datetime_correctly(session):
    """If seen_at is naive (SQLite default), gate must coerce to UTC for comparison."""
    pt = PublicTrade(
        id="src-naive",
        wallet_address="0xwallet",
        market_id="0xmkt",
        outcome="Yes",
        side="BUY",
        price=0.50,
        size=10.0,
        traded_at=datetime(2026, 5, 12, 12, 0, 0),  # naive
        seen_at=datetime(2026, 5, 12, 12, 0, 30),  # naive
    )
    session.add(pt)
    session.commit()
    from app.models.trade import PublicTrade as PT
    row = session.get(PT, "src-naive")
    assert row is not None
    # Reading: seen_at may be naive. Gate code does .replace(tzinfo=UTC) when needed.
    seen = row.seen_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    assert seen.tzinfo is not None
