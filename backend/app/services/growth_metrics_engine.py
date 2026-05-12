"""Growth metrics service (Phase A complement — 2026-05-11).

Computes risk-adjusted growth metrics over a window of closed paper trades.
Goes beyond raw PnL/PF to provide Sharpe/Sortino/Calmar/CAGR and drawdown
metrics so the operator sees if growth is *sustainable* and not just lucky.

Pure function: takes Session + window_hours → returns GrowthHealthReport.
Filters trades opened ≥ strict_cutover_at if cutover set (cohérent avec
compute_paper_pnl).

Used by:
  - GET /edge/growth-metrics  (operator dashboard)
  - Phase B baseline report (sustainability go/no-go)
  - Phase F scaling palier triggers (Calmar ≥ X required to ramp)

Métriques (formules):

  CAGR (annualisé):
    cagr = (end_capital / start_capital) ^ (365.25 / days) - 1

  Sharpe (par jour, annualisé sqrt(365)):
    sharpe = mean(daily_returns) / stddev(daily_returns) × sqrt(365)

  Sortino (downside-only stddev):
    sortino = mean(daily_returns) / downside_stddev × sqrt(365)

  Calmar (CAGR / max DD):
    calmar = cagr / max_drawdown_pct

  Days under water:
    pct of days where capital < HWM_at_that_date

  Time to recover from worst DD (jours moyens depuis trough → HWM)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import sqrt
from statistics import mean, stdev
from typing import Any

from sqlmodel import Session, select

from app.models.bot import BotState
from app.models.trade import PaperTrade

TRADING_DAYS_PER_YEAR = 365.25
ALERT_PF_MIN = 1.5
ALERT_DD_MAX_PCT = 0.10  # 10% max DD
ALERT_SHARPE_MIN = 1.0
ALERT_CALMAR_MIN = 0.5


@dataclass
class GrowthHealthReport:
    window_hours: float
    sample_size: int
    start_capital: float
    end_capital: float
    realized_pnl: float
    profit_factor: float | None
    win_rate: float | None
    cagr: float | None
    sharpe_annualized: float | None
    sortino_annualized: float | None
    calmar: float | None
    max_drawdown_pct: float | None
    max_drawdown_abs: float | None
    days_under_water_pct: float | None
    time_to_recover_days_avg: float | None
    worst_trade_pnl: float | None
    worst_day_pnl: float | None
    rolling_growth_velocity_7d: float | None
    rolling_growth_velocity_14d: float | None
    rolling_growth_velocity_30d: float | None
    classification: str
    alerts: list[str]


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _filter_post_cutover(
    session: Session, trades: list[PaperTrade]
) -> list[PaperTrade]:
    state = session.get(BotState, 1)
    cutover = getattr(state, "strict_cutover_at", None) if state else None
    if cutover is None:
        return trades
    cutover = _ensure_aware(cutover)
    return [
        t for t in trades
        if (_ensure_aware(t.opened_at) or datetime.min.replace(tzinfo=UTC)) >= cutover
    ]


def _equity_curve(
    trades: list[PaperTrade], start_capital: float
) -> list[tuple[datetime, float]]:
    """Build chronological equity curve from start_capital + cumulative PnL
    of closed trades."""
    sorted_trades = sorted(
        [t for t in trades if t.closed_at is not None and t.realized_pnl is not None],
        key=lambda t: _ensure_aware(t.closed_at) or datetime.min.replace(tzinfo=UTC),
    )
    equity = start_capital
    curve: list[tuple[datetime, float]] = []
    for t in sorted_trades:
        equity += float(t.realized_pnl or 0.0)
        ts = _ensure_aware(t.closed_at)
        if ts is not None:
            curve.append((ts, round(equity, 2)))
    return curve


def _max_drawdown(curve: list[tuple[datetime, float]]) -> tuple[float, float, datetime | None, datetime | None]:
    """Returns (max_dd_pct, max_dd_abs, peak_date, trough_date)."""
    if not curve:
        return (0.0, 0.0, None, None)
    peak = curve[0][1]
    peak_date = curve[0][0]
    max_dd_pct = 0.0
    max_dd_abs = 0.0
    cur_peak_date = peak_date
    final_peak_date: datetime | None = None
    final_trough_date: datetime | None = None
    for ts, eq in curve:
        if eq > peak:
            peak = eq
            cur_peak_date = ts
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd_pct:
                max_dd_pct = dd
                max_dd_abs = peak - eq
                final_peak_date = cur_peak_date
                final_trough_date = ts
    return (max_dd_pct, max_dd_abs, final_peak_date, final_trough_date)


def _daily_returns(curve: list[tuple[datetime, float]]) -> list[float]:
    """Group equity into daily buckets, compute daily returns."""
    if not curve:
        return []
    by_day: dict[str, float] = {}
    for ts, eq in curve:
        key = ts.date().isoformat()
        by_day[key] = eq  # last equity of the day
    sorted_keys = sorted(by_day.keys())
    if len(sorted_keys) < 2:
        return []
    returns: list[float] = []
    for i in range(1, len(sorted_keys)):
        prev = by_day[sorted_keys[i - 1]]
        cur = by_day[sorted_keys[i]]
        if prev > 0:
            returns.append((cur - prev) / prev)
    return returns


def _sharpe(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    sd = stdev(returns)
    if sd == 0:
        return None
    return mean(returns) / sd * sqrt(TRADING_DAYS_PER_YEAR)


def _sortino(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    downside = [r for r in returns if r < 0]
    if len(downside) < 2:
        return None
    dsd = stdev(downside)
    if dsd == 0:
        return None
    return mean(returns) / dsd * sqrt(TRADING_DAYS_PER_YEAR)


def _cagr(start: float, end: float, days: float) -> float | None:
    if start <= 0 or days <= 0:
        return None
    return (end / start) ** (TRADING_DAYS_PER_YEAR / days) - 1


def _days_under_water(curve: list[tuple[datetime, float]]) -> float | None:
    if not curve:
        return None
    peak = curve[0][1]
    by_day: dict[str, tuple[float, float]] = {}  # day → (peak_at_that_day, eq_at_that_day)
    for ts, eq in curve:
        if eq > peak:
            peak = eq
        key = ts.date().isoformat()
        by_day[key] = (peak, eq)
    if not by_day:
        return None
    under = sum(1 for p, e in by_day.values() if e < p)
    return under / len(by_day)


def _time_to_recover(curve: list[tuple[datetime, float]]) -> float | None:
    """Average days from trough → next HWM. Only counts complete recoveries."""
    if len(curve) < 2:
        return None
    peak = curve[0][1]
    peak_date = curve[0][0]
    in_dd = False
    trough_date: datetime | None = None
    recoveries: list[float] = []
    for ts, eq in curve:
        if eq > peak:
            if in_dd and trough_date is not None:
                recoveries.append((ts - trough_date).total_seconds() / 86400)
            peak = eq
            peak_date = ts
            in_dd = False
            trough_date = None
        elif eq < peak:
            if not in_dd:
                in_dd = True
                trough_date = ts
            elif trough_date is None or eq < curve[curve.index((ts, eq)) - 1][1]:
                trough_date = ts
    return mean(recoveries) if recoveries else None


def _profit_factor(trades: list[PaperTrade]) -> float | None:
    gross_win = sum(float(t.realized_pnl or 0) for t in trades if (t.realized_pnl or 0) > 0)
    gross_loss = abs(sum(float(t.realized_pnl or 0) for t in trades if (t.realized_pnl or 0) < 0))
    if gross_loss == 0:
        return None if gross_win == 0 else float("inf")
    return gross_win / gross_loss


def _win_rate(trades: list[PaperTrade]) -> float | None:
    if not trades:
        return None
    wins = sum(1 for t in trades if (t.realized_pnl or 0) > 0)
    return wins / len(trades)


def _rolling_velocity(curve: list[tuple[datetime, float]], days: int) -> float | None:
    """Average daily growth % over the last N days."""
    if not curve or len(curve) < 2:
        return None
    cutoff = datetime.now(UTC) - timedelta(days=days)
    recent = [pt for pt in curve if pt[0] >= cutoff]
    if len(recent) < 2:
        return None
    start_eq = recent[0][1]
    end_eq = recent[-1][1]
    if start_eq <= 0:
        return None
    return ((end_eq - start_eq) / start_eq) / days


def _classify(report: dict[str, Any]) -> tuple[str, list[str]]:
    """Risk-adjusted growth classification + alerts list."""
    alerts: list[str] = []
    pf = report.get("profit_factor")
    dd_pct = report.get("max_drawdown_pct")
    sharpe = report.get("sharpe_annualized")
    calmar = report.get("calmar")

    if pf is not None and pf < ALERT_PF_MIN:
        alerts.append(f"PF {pf:.2f} < {ALERT_PF_MIN}")
    if dd_pct is not None and dd_pct > ALERT_DD_MAX_PCT:
        alerts.append(f"max_DD {dd_pct*100:.1f}% > {ALERT_DD_MAX_PCT*100:.0f}%")
    if sharpe is not None and sharpe < ALERT_SHARPE_MIN:
        alerts.append(f"Sharpe {sharpe:.2f} < {ALERT_SHARPE_MIN}")
    if calmar is not None and calmar < ALERT_CALMAR_MIN:
        alerts.append(f"Calmar {calmar:.2f} < {ALERT_CALMAR_MIN}")

    if report["sample_size"] == 0:
        return ("UNKNOWN", ["no closed trades in window"])
    if not alerts:
        return ("HEALTHY", [])
    if len(alerts) == 1:
        return ("WATCH", alerts)
    if len(alerts) >= 3:
        return ("CRITICAL", alerts)
    return ("DEGRADED", alerts)


def compute_growth_health_report(
    session: Session, *, window_hours: float = 168.0,
) -> GrowthHealthReport:
    """Pure function — only reads DB. Easy to unit-test."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    state = session.get(BotState, 1)
    start_capital = float((state.paper_capital if state else 100.0) or 100.0)

    all_closed = list(session.exec(
        select(PaperTrade).where(PaperTrade.status == "closed")
    ).all())
    in_window = [
        t for t in all_closed
        if (_ensure_aware(t.closed_at) or datetime.min.replace(tzinfo=UTC)) >= cutoff
    ]
    in_window = _filter_post_cutover(session, in_window)

    curve = _equity_curve(in_window, start_capital)
    end_capital = curve[-1][1] if curve else start_capital
    realized_pnl = end_capital - start_capital

    pf = _profit_factor(in_window)
    wr = _win_rate(in_window)

    if curve and len(curve) >= 2:
        days = (curve[-1][0] - curve[0][0]).total_seconds() / 86400
        cagr = _cagr(start_capital, end_capital, days)
    else:
        cagr = None

    daily_ret = _daily_returns(curve)
    sharpe = _sharpe(daily_ret)
    sortino = _sortino(daily_ret)

    dd_pct, dd_abs, _, _ = _max_drawdown(curve)
    max_dd_pct = dd_pct if dd_pct > 0 else None
    max_dd_abs_v = dd_abs if dd_abs > 0 else None

    calmar = (cagr / max_dd_pct) if (cagr is not None and max_dd_pct) else None
    duw = _days_under_water(curve)
    ttr = _time_to_recover(curve)

    worst_trade = min((float(t.realized_pnl or 0) for t in in_window), default=None)
    by_day_pnl: dict[str, float] = defaultdict(float)
    for t in in_window:
        d = _ensure_aware(t.closed_at)
        if d:
            by_day_pnl[d.date().isoformat()] += float(t.realized_pnl or 0)
    worst_day = min(by_day_pnl.values(), default=None) if by_day_pnl else None

    report_data = dict(
        window_hours=window_hours,
        sample_size=len(in_window),
        start_capital=round(start_capital, 4),
        end_capital=round(end_capital, 4),
        realized_pnl=round(realized_pnl, 4),
        profit_factor=round(pf, 4) if pf is not None and pf != float("inf") else pf,
        win_rate=round(wr, 4) if wr is not None else None,
        cagr=round(cagr, 4) if cagr is not None else None,
        sharpe_annualized=round(sharpe, 4) if sharpe is not None else None,
        sortino_annualized=round(sortino, 4) if sortino is not None else None,
        calmar=round(calmar, 4) if calmar is not None else None,
        max_drawdown_pct=round(max_dd_pct, 4) if max_dd_pct is not None else None,
        max_drawdown_abs=round(max_dd_abs_v, 4) if max_dd_abs_v is not None else None,
        days_under_water_pct=round(duw, 4) if duw is not None else None,
        time_to_recover_days_avg=round(ttr, 2) if ttr is not None else None,
        worst_trade_pnl=round(worst_trade, 4) if worst_trade is not None else None,
        worst_day_pnl=round(worst_day, 4) if worst_day is not None else None,
        rolling_growth_velocity_7d=_rolling_velocity(curve, 7),
        rolling_growth_velocity_14d=_rolling_velocity(curve, 14),
        rolling_growth_velocity_30d=_rolling_velocity(curve, 30),
    )

    classification, alerts = _classify(report_data)
    return GrowthHealthReport(
        **report_data,
        classification=classification,
        alerts=alerts,
    )


def growth_metrics_payload(
    session: Session, *, window_hours: float = 168.0,
) -> dict[str, Any]:
    return asdict(compute_growth_health_report(session, window_hours=window_hours))
