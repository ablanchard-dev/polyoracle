"""M5 throughput classifier (A-INSTR Phase A — 2026-05-11).

Compute utilization metrics + classify the throughput bottleneck so the
operator knows which lever to pull next:

  Slot-bound        → max_pos_utilization_p95 ≥ 0.90 AND slot_block_rate ≥ 0.10
  Capital-bound     → capital_utilization_p95 ≥ 0.90 AND max_pos util < 0.90
  Opportunity-bound → max_pos_utilization_p95 < 0.70 AND fresh_signal_rate low
  Latency-bound     → stale/backfill rate > 0.25
  Risk-gate-bound   → rejects dominated by spread/liquidity/orderbook/copy_edge

Pure-function API: pass in a Session + window_hours → get back a dict
with metrics + bound classification. No global state, easy to unit-test.

Used by:
  - GET /observability/utilization  (operator dashboard)
  - Phase B baseline report
  - Phase C levers selection (drives ordered C1/C2/C3/C4/C5)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any

from sqlmodel import Session, select

from app.models.bot import BotState
from app.models.signal import Signal
from app.models.trade import NoTradeDecision, PaperTrade

# --------- Thresholds (locked 2026-05-11) ---------

SLOT_UTIL_P95_THRESHOLD = 0.90
SLOT_BLOCK_RATE_THRESHOLD = 0.10
CAPITAL_UTIL_P95_THRESHOLD = 0.90
OPPORTUNITY_UTIL_P95_THRESHOLD = 0.70
LATENCY_STALE_RATE_THRESHOLD = 0.25

# Reason-code groups for rejection breakdown
RISK_GATE_CODES = frozenset({
    "WIDE_SPREAD",
    "LOW_LIQUIDITY",
    "BAD_ORDERBOOK",
    "INSUFFICIENT_DATA",
    "NO_COPYABLE_EDGE",
})
SLOT_CODES = frozenset({"TOO_MANY_POSITIONS", "POSITION_LIMIT"})
CAPITAL_CODES = frozenset({"INSUFFICIENT_CAPITAL", "EXPOSURE_LIMIT"})
LATENCY_CODES = frozenset({
    "LATE_ENTRY",
    "STALE_SIGNAL_BACKFILL",
    "PRICE_DETERIORATION",
})


@dataclass
class UtilizationMetrics:
    window_hours: float
    sample_size: int  # = NoTradeDecision count in window
    open_positions_now: int
    max_open_positions: int
    max_pos_utilization_avg: float
    max_pos_utilization_p95: float
    slot_block_rate: float
    capital_utilization_avg: float
    capital_utilization_p95: float
    idle_capital_pct: float
    opportunity_fill_rate: float
    fresh_signal_rate_per_hour: float
    stale_backfill_rate: float
    risk_gate_rate: float
    rejection_breakdown: dict[str, int]
    bound_classification: str
    bound_reasoning: str


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct * (len(s) - 1)))))
    return s[k]


def _hourly_open_positions_series(
    session: Session, window_hours: float
) -> list[int]:
    """Hourly snapshot of open positions over the window. Approximation:
    for each hour bucket, count PaperTrade rows that were open during
    that hour (opened_at <= bucket_end AND (closed_at > bucket_end OR closed_at IS NULL)).
    """
    now = datetime.now(UTC)
    start = now - timedelta(hours=window_hours)
    series: list[int] = []
    bucket_count = max(1, int(window_hours))
    for i in range(bucket_count):
        bucket_end = start + timedelta(hours=i + 1)
        count = 0
        for trade in session.exec(select(PaperTrade)).all():
            opened = trade.opened_at
            if opened is None:
                continue
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            if opened > bucket_end:
                continue
            closed = trade.closed_at
            if closed is None:
                count += 1
                continue
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=UTC)
            if closed > bucket_end:
                count += 1
        series.append(count)
    return series


def _classify_bound(
    *,
    util_p95: float,
    slot_block_rate: float,
    capital_util_p95: float,
    stale_rate: float,
    risk_gate_rate: float,
    fresh_signal_rate: float,
    rejection_breakdown: dict[str, int],
    sample_size: int,
) -> tuple[str, str]:
    """Return (bound_name, human_readable_reasoning).

    Order of precedence: slot > capital > latency > risk-gate > opportunity.
    Empty sample → UNKNOWN to avoid false signals.
    """
    if sample_size == 0:
        return ("UNKNOWN", "No NoTradeDecision in window — too early to classify")

    if util_p95 >= SLOT_UTIL_P95_THRESHOLD and slot_block_rate >= SLOT_BLOCK_RATE_THRESHOLD:
        return (
            "slot-bound",
            f"max_pos_utilization_p95={util_p95:.2f} ≥ {SLOT_UTIL_P95_THRESHOLD} "
            f"AND slot_block_rate={slot_block_rate:.2f} ≥ {SLOT_BLOCK_RATE_THRESHOLD}",
        )
    if capital_util_p95 >= CAPITAL_UTIL_P95_THRESHOLD and util_p95 < SLOT_UTIL_P95_THRESHOLD:
        return (
            "capital-bound",
            f"capital_utilization_p95={capital_util_p95:.2f} ≥ {CAPITAL_UTIL_P95_THRESHOLD} "
            f"AND max_pos util={util_p95:.2f} < {SLOT_UTIL_P95_THRESHOLD}",
        )
    if stale_rate > LATENCY_STALE_RATE_THRESHOLD:
        return (
            "latency-bound",
            f"stale_backfill_rate={stale_rate:.2f} > {LATENCY_STALE_RATE_THRESHOLD}",
        )
    if risk_gate_rate >= 0.50:
        top_code = max(rejection_breakdown.items(), key=lambda kv: kv[1])[0] if rejection_breakdown else "?"
        return (
            "risk-gate-bound",
            f"risk_gate_rate={risk_gate_rate:.2f} dominates rejects (top={top_code})",
        )
    if util_p95 < OPPORTUNITY_UTIL_P95_THRESHOLD:
        return (
            "opportunity-bound",
            f"max_pos_utilization_p95={util_p95:.2f} < {OPPORTUNITY_UTIL_P95_THRESHOLD} "
            f"AND fresh_signal_rate={fresh_signal_rate:.2f}/h",
        )
    return ("balanced", "No single bound dominates — system in healthy steady state")


def compute_utilization_metrics(
    session: Session, *, window_hours: float = 24.0,
) -> UtilizationMetrics:
    """Compute all M5 utilization + throughput metrics for the window.

    Pure function — only reads from the DB session, no global state.
    """
    now = datetime.now(UTC)
    start = now - timedelta(hours=window_hours)
    state = session.get(BotState, 1)
    paper_capital = float((state.paper_capital if state else 0) or 0)

    # Resolve current tier max_pos via capital_allocator (same source-of-truth)
    tier: dict[str, Any] = {}
    max_open_positions = 0
    try:
        from app.services.capital_allocator import _resolve_tier
        tier = _resolve_tier(paper_capital)
        max_open_positions = int(tier.get("max_open_positions_cap") or 0)
    except Exception:
        pass

    # 1. open_positions snapshot (current)
    open_now = len(list(session.exec(
        select(PaperTrade).where(PaperTrade.status == "open")
    ).all()))

    # 2. max_pos utilization series (hourly)
    series = _hourly_open_positions_series(session, window_hours)
    if max_open_positions > 0 and series:
        util_series = [s / max_open_positions for s in series]
        util_avg = mean(util_series)
        util_p95 = _percentile(util_series, 0.95)
    else:
        util_avg = util_p95 = 0.0

    # 3. NoTradeDecision breakdown (window)
    no_trades = session.exec(
        select(NoTradeDecision).where(NoTradeDecision.created_at >= start)
    ).all()
    rejection_breakdown = Counter(nt.reason_code for nt in no_trades)
    sample_size = len(no_trades)

    slot_count = sum(rejection_breakdown.get(c, 0) for c in SLOT_CODES)
    capital_count = sum(rejection_breakdown.get(c, 0) for c in CAPITAL_CODES)
    latency_count = sum(rejection_breakdown.get(c, 0) for c in LATENCY_CODES)
    risk_gate_count = sum(rejection_breakdown.get(c, 0) for c in RISK_GATE_CODES)

    slot_block_rate = (slot_count / sample_size) if sample_size else 0.0
    stale_backfill_rate = (latency_count / sample_size) if sample_size else 0.0
    risk_gate_rate = (risk_gate_count / sample_size) if sample_size else 0.0

    # 4. capital utilization (= sum open notional / paper_capital × tier exposure cap)
    open_notional = sum(
        float(t.notional_usd or 0)
        for t in session.exec(
            select(PaperTrade).where(PaperTrade.status == "open")
        ).all()
    )
    tier_expo_cap = float(tier.get("max_total_exposure_cap") or 1.0)
    cap_limit = max(paper_capital * tier_expo_cap, 1.0)
    capital_util_avg = min(1.0, open_notional / cap_limit)
    capital_util_p95 = capital_util_avg  # static snapshot proxy until time-series tracking
    idle_capital_pct = max(0.0, 1.0 - capital_util_avg)

    # 5. fresh signal rate (PAPER_TRADE signals in window / hours)
    paper_signals = session.exec(
        select(Signal).where(
            Signal.created_at >= start,
            Signal.decision == "PAPER_TRADE",
        )
    ).all()
    fresh_signal_rate = len(list(paper_signals)) / max(window_hours, 1.0)

    # 6. opportunity fill rate (= opened / paper_eligible)
    opened_in_window = len(list(session.exec(
        select(PaperTrade).where(PaperTrade.opened_at >= start)
    ).all()))
    eligible_proxy = opened_in_window + sample_size  # approximate denominator
    opportunity_fill_rate = (opened_in_window / eligible_proxy) if eligible_proxy else 0.0

    bound, reasoning = _classify_bound(
        util_p95=util_p95,
        slot_block_rate=slot_block_rate,
        capital_util_p95=capital_util_p95,
        stale_rate=stale_backfill_rate,
        risk_gate_rate=risk_gate_rate,
        fresh_signal_rate=fresh_signal_rate,
        rejection_breakdown=dict(rejection_breakdown),
        sample_size=sample_size,
    )

    return UtilizationMetrics(
        window_hours=window_hours,
        sample_size=sample_size,
        open_positions_now=open_now,
        max_open_positions=max_open_positions,
        max_pos_utilization_avg=round(util_avg, 4),
        max_pos_utilization_p95=round(util_p95, 4),
        slot_block_rate=round(slot_block_rate, 4),
        capital_utilization_avg=round(capital_util_avg, 4),
        capital_utilization_p95=round(capital_util_p95, 4),
        idle_capital_pct=round(idle_capital_pct, 4),
        opportunity_fill_rate=round(opportunity_fill_rate, 4),
        fresh_signal_rate_per_hour=round(fresh_signal_rate, 4),
        stale_backfill_rate=round(stale_backfill_rate, 4),
        risk_gate_rate=round(risk_gate_rate, 4),
        rejection_breakdown=dict(rejection_breakdown),
        bound_classification=bound,
        bound_reasoning=reasoning,
    )


def utilization_payload(session: Session, *, window_hours: float = 24.0) -> dict[str, Any]:
    """Convenience wrapper for FastAPI route — returns a dict (not dataclass)."""
    return asdict(compute_utilization_metrics(session, window_hours=window_hours))
