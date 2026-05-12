"""A-INSTR M5 throughput classifier tests (2026-05-11).

Verifies utilization metrics + 5-bound classification logic against
synthetic DB fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine

from app.models.bot import BotState
from app.models.trade import NoTradeDecision, PaperTrade
from app.services.throughput_classifier import (
    SLOT_BLOCK_RATE_THRESHOLD,
    SLOT_UTIL_P95_THRESHOLD,
    compute_utilization_metrics,
)


def _session() -> Session:
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def _seed_state(session: Session, capital: float = 100.0) -> None:
    session.add(BotState(id=1, mode="PAPER", running=True, paper_capital=capital))
    session.commit()


def _seed_open_positions(session: Session, n: int) -> None:
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(n):
        session.add(PaperTrade(
            id=f"open-{i}",
            market_id=f"0xmkt-{i}",
            outcome="Yes",
            side="BUY",
            quantity=10.0,
            average_price=0.50,
            status="open",
            opened_at=base,
            notional_usd=5.0,
        ))
    session.commit()


def _seed_no_trade(session: Session, code: str, n: int) -> None:
    base = datetime.now(UTC) - timedelta(minutes=30)
    for i in range(n):
        session.add(NoTradeDecision(
            id=f"nt-{code}-{i}",
            reason_code=code,
            created_at=base,
        ))
    session.commit()


def test_unknown_classification_with_empty_db():
    """No trades, no rejects → UNKNOWN bound (avoid false signals)."""
    with _session() as s:
        _seed_state(s)
        m = compute_utilization_metrics(s, window_hours=24.0)
        assert m.bound_classification == "UNKNOWN"
        assert m.sample_size == 0


def test_slot_bound_classification():
    """High max_pos utilization + slot blocks → slot-bound."""
    with _session() as s:
        _seed_state(s, capital=100.0)  # NANO tier max_pos=24
        _seed_open_positions(s, 24)  # 100% utilization
        _seed_no_trade(s, "TOO_MANY_POSITIONS", 5)
        _seed_no_trade(s, "WIDE_SPREAD", 30)  # other code to bring sample size up
        m = compute_utilization_metrics(s, window_hours=2.0)
        assert m.bound_classification == "slot-bound"
        assert m.max_pos_utilization_p95 >= SLOT_UTIL_P95_THRESHOLD
        assert m.slot_block_rate >= SLOT_BLOCK_RATE_THRESHOLD


def test_risk_gate_bound_classification():
    """Rejects dominated by risk-gate codes → risk-gate-bound."""
    with _session() as s:
        _seed_state(s, capital=100.0)
        _seed_no_trade(s, "WIDE_SPREAD", 50)
        _seed_no_trade(s, "BAD_ORDERBOOK", 30)
        _seed_no_trade(s, "LATE_ENTRY", 5)
        m = compute_utilization_metrics(s, window_hours=2.0)
        assert m.bound_classification == "risk-gate-bound"
        assert m.risk_gate_rate >= 0.50


def test_latency_bound_classification():
    """Stale/backfill rejects > 25% → latency-bound."""
    with _session() as s:
        _seed_state(s, capital=100.0)
        _seed_no_trade(s, "STALE_SIGNAL_BACKFILL", 60)
        _seed_no_trade(s, "WIDE_SPREAD", 40)
        m = compute_utilization_metrics(s, window_hours=2.0)
        assert m.bound_classification == "latency-bound"
        assert m.stale_backfill_rate > 0.25


def test_opportunity_bound_classification():
    """Low utilization + low fresh signals → opportunity-bound.
    Uses 'neutral' reject codes (not in any specific bucket) to ensure no
    other classification dominates."""
    with _session() as s:
        _seed_state(s, capital=100.0)  # NANO max_pos=24
        _seed_open_positions(s, 5)  # 5/24 = 20% utilization
        # Use codes outside SLOT/CAPITAL/LATENCY/RISK_GATE buckets
        _seed_no_trade(s, "WALLET_TIER", 5)
        _seed_no_trade(s, "AUDIT_DECISION_NOT_PAPER", 5)
        m = compute_utilization_metrics(s, window_hours=2.0)
        assert m.bound_classification == "opportunity-bound"
        assert m.max_pos_utilization_p95 < 0.70


def test_rejection_breakdown_is_a_counter():
    with _session() as s:
        _seed_state(s)
        _seed_no_trade(s, "WIDE_SPREAD", 3)
        _seed_no_trade(s, "BAD_ORDERBOOK", 2)
        m = compute_utilization_metrics(s, window_hours=1.0)
        assert m.rejection_breakdown.get("WIDE_SPREAD") == 3
        assert m.rejection_breakdown.get("BAD_ORDERBOOK") == 2
        assert m.sample_size == 5


def test_capital_utilization_uses_tier_exposure_cap():
    """Open notional / (capital × tier expo cap) drives capital_utilization."""
    with _session() as s:
        _seed_state(s, capital=100.0)  # NANO expo_cap=0.75 → limit ≈ 75
        # 6 open positions × 5 USDC notional = 30 → 30/75 = 0.40
        _seed_open_positions(s, 6)
        m = compute_utilization_metrics(s, window_hours=1.0)
        assert m.capital_utilization_avg > 0.0
        assert m.capital_utilization_avg <= 1.0


def test_window_hours_scales_metrics():
    """Different windows produce different time-series buckets."""
    with _session() as s:
        _seed_state(s)
        _seed_no_trade(s, "WIDE_SPREAD", 10)
        m_short = compute_utilization_metrics(s, window_hours=1.0)
        m_long = compute_utilization_metrics(s, window_hours=24.0)
        # Both see all 10 rejects (created in last 30min)
        assert m_short.sample_size == 10
        assert m_long.sample_size == 10
        # But fresh_signal_rate divides by hours
        assert m_short.fresh_signal_rate_per_hour >= m_long.fresh_signal_rate_per_hour
