"""M3 wallet weighting engine (Phase C C1 — 2026-05-11).

Compute per-wallet weight for use as:
  Phase 1: slot priority (no sizing impact, just ordering)
  Phase 2: light sizing multiplier R × weight, clamp [0.75R, 1.25R]
  Phase 3: wider clamp [0.5R, 1.5R] after 1000+ confirmed trades

Formula (locked 2026-05-11 by Round 2 review tranchage):

  raw = 0.35 × long_prior_WR_lower_bound
      + 0.25 × EWMA_14d_PnL_per_trade
      + 0.15 × copy_efficiency
      + 0.10 × consistency_score
      + 0.10 × bucket_edge_score
      + 0.05 × activity_score
      - penalties (rage_trade, concentration, illiquidity, tail_loss)

Bayesian regularization (avoid overfitting on low sample):
  weight = (n_recent / 100) × raw + (1 - n_recent / 100) × base_prior
  with n_recent capped at 100.

Cap (paper/live progressive):
  paper:    [0.5, 1.5]
  live (initial 1000+ trades): [0.7, 1.25]

Activation policy:
  Phase 1 (slot priority only):  immediately after baseline B
  Phase 2 (light sizing × weight): after 100+ strict trades + go Phase B
  Phase 3 (wider clamp):           after 1000+ strict trades

Pure function: takes a Session + window_hours → returns dict[wallet → weight].
NOT activated in runtime yet — file is just the engine. Caller wiring will
happen in Phase C C1 implementation post-baseline.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from math import exp, log, sqrt
from typing import Any

from sqlmodel import Session, select

from app.models.trade import PaperTrade, PublicTrade
from app.models.wallet import MarketFirstWalletRecord

# --------- Locked weights & caps (2026-05-11) ---------

W_PRIOR = 0.35
W_EWMA_14D = 0.25
W_COPY_EFF = 0.15
W_CONSISTENCY = 0.10
W_BUCKET_EDGE = 0.10
W_ACTIVITY = 0.05

PAPER_CAP_MIN = 0.5
PAPER_CAP_MAX = 1.5
LIVE_INITIAL_CAP_MIN = 0.7
LIVE_INITIAL_CAP_MAX = 1.25

# Bayesian regularization
N_RECENT_PRIOR_TRANSITION = 100  # at n_recent=100, weight = 100% raw, 0% base
BASE_PRIOR_NEUTRAL = 1.0  # neutral starting weight before sample accumulates
MIN_RECENT_FOR_BOOST = 30  # below 30 recent trades, weight clamped at 1.0

# EWMA decay
EWMA_LAMBDA_DAILY = 0.94  # daily decay
EWMA_LAMBDA_PER_TRADE = 0.98  # per-trade decay (alternative)

# Concentration limit (anti-overfit)
MAX_TRADE_SHARE_PER_WALLET = 0.15  # 15% of trades / day
MAX_PNL_SHARE_PER_WALLET = 0.20  # 20% of daily PnL

# Penalty sources
PENALTY_RAGE_TRADE = 0.30  # subtract per detected rage trade in window
PENALTY_TAIL_LOSS = 0.20  # if max single-trade loss > 5×R
PENALTY_CONCENTRATION = 0.15  # if wallet > MAX_TRADE_SHARE


@dataclass
class WalletWeight:
    wallet: str
    n_recent: int
    long_prior: float
    ewma_14d: float
    copy_efficiency: float
    consistency: float
    bucket_edge: float
    activity: float
    penalties: float
    raw_weight: float
    regularized_weight: float
    final_weight: float  # after cap clamp
    notes: list[str] = field(default_factory=list)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound (95% by default).
    Pénalise les sample size faibles, n=10 wins/10 → ~0.69 lower bound."""
    if n == 0:
        return 0.0
    p = wins / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / denominator)


def _ewma_pnl_per_trade(
    trades: list[PaperTrade], lambda_per_trade: float = EWMA_LAMBDA_PER_TRADE
) -> float:
    """Exponentially weighted moving average of pnl per trade. Most recent
    trades weighted exponentially more."""
    if not trades:
        return 0.0
    sorted_t = sorted(trades, key=lambda t: t.closed_at or t.opened_at or datetime.min)
    ewma = 0.0
    for i, t in enumerate(reversed(sorted_t)):
        weight = lambda_per_trade ** i
        notional = max(float(t.notional_usd or 1.0), 1.0)
        pnl_per_unit = float(t.realized_pnl or 0.0) / notional
        ewma += weight * pnl_per_unit
    return ewma / max(len(sorted_t), 1)


def _consistency_score(trades: list[PaperTrade]) -> float:
    """% jours positifs - max losing streak penalty. 0 = bad, 1 = excellent."""
    if not trades:
        return 0.0
    daily_pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        d = t.closed_at or t.opened_at
        if d is None:
            continue
        d = _ensure_aware(d)
        if d is None:
            continue
        key = d.date().isoformat()
        daily_pnl[key] += float(t.realized_pnl or 0.0)
    if not daily_pnl:
        return 0.0
    pos_days = sum(1 for v in daily_pnl.values() if v > 0)
    pos_ratio = pos_days / len(daily_pnl)

    # Max losing streak (consecutive negative trades)
    sorted_t = sorted(trades, key=lambda t: t.closed_at or t.opened_at or datetime.min)
    max_streak = current_streak = 0
    for t in sorted_t:
        if (t.realized_pnl or 0) < 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    streak_penalty = min(0.5, max_streak / 20)  # streak 20 = -0.5

    return max(0.0, pos_ratio - streak_penalty)


def _bucket_edge_score(
    wallet_trades_by_bucket_cat: dict[tuple[str, str], list[PaperTrade]],
) -> float:
    """Score per (duration_bucket × category) edge. Higher when wallet has
    consistent positive edge across multiple buckets."""
    if not wallet_trades_by_bucket_cat:
        return 0.0
    positive_buckets = sum(
        1
        for trades in wallet_trades_by_bucket_cat.values()
        if sum(float(t.realized_pnl or 0) for t in trades) > 0
    )
    return positive_buckets / max(len(wallet_trades_by_bucket_cat), 1)


def _activity_score(record: MarketFirstWalletRecord | None, recent_trades_n: int) -> float:
    """Activity = blend of recent_activity_score (DB) + actually-detected
    recent trades count."""
    base = 0.0
    if record and getattr(record, "recent_activity_score", None) is not None:
        base = float(record.recent_activity_score) / 100.0
    detected = min(1.0, recent_trades_n / 50)  # 50+ trades in window = max
    return (base + detected) / 2


def _detect_penalties(trades: list[PaperTrade]) -> tuple[float, list[str]]:
    """Sum of penalty sources triggered + list of triggered codes."""
    penalty = 0.0
    notes: list[str] = []
    if not trades:
        return (0.0, notes)
    pnls = [float(t.realized_pnl or 0.0) for t in trades]
    notionals = [max(float(t.notional_usd or 1.0), 1.0) for t in trades]
    if not pnls:
        return (0.0, notes)
    # Tail loss = single trade -5R or worse (R = 2% of avg notional)
    avg_notional = sum(notionals) / len(notionals)
    r_value = 0.02 * avg_notional
    worst = min(pnls)
    if worst < -5 * r_value and r_value > 0:
        penalty += PENALTY_TAIL_LOSS
        notes.append(f"tail_loss({worst:.2f}<-5R)")
    # Rage trade = >3 consecutive losing trades all >2R loss
    consec_big_loss = 0
    for p in trades:
        pnl = float(p.realized_pnl or 0.0)
        notional = max(float(p.notional_usd or 1.0), 1.0)
        if pnl < -2 * 0.02 * notional:
            consec_big_loss += 1
            if consec_big_loss >= 3:
                penalty += PENALTY_RAGE_TRADE
                notes.append("rage_trade(3+_big_losses)")
                break
        else:
            consec_big_loss = 0
    return (penalty, notes)


def _compute_one_wallet_weight(
    *,
    wallet: str,
    record: MarketFirstWalletRecord | None,
    recent_trades: list[PaperTrade],
    copy_efficiency: float,
    cap_min: float,
    cap_max: float,
) -> WalletWeight:
    notes: list[str] = []

    # Long prior — Wilson lower bound on resolved W+L
    if record:
        w = int(getattr(record, "resolved_winning_markets", 0) or 0)
        l = int(getattr(record, "resolved_losing_markets", 0) or 0)
        long_prior = _wilson_lower_bound(w, w + l)
    else:
        long_prior = 0.5

    ewma_14d = _ewma_pnl_per_trade(recent_trades)

    consistency = _consistency_score(recent_trades)

    # Bucket edge — group trades by (bucket, category)
    by_bc: dict[tuple[str, str], list[PaperTrade]] = defaultdict(list)
    for t in recent_trades:
        # Bucket from holding minutes; category from market_id (best-effort)
        opened = _ensure_aware(t.opened_at)
        closed = _ensure_aware(t.closed_at)
        minutes = ((closed - opened).total_seconds() / 60) if (opened and closed) else 0
        if minutes <= 5:
            bucket = "ULTRA_SHORT"
        elif minutes <= 30:
            bucket = "VERY_SHORT"
        elif minutes <= 120:
            bucket = "SHORT"
        elif minutes <= 1440:
            bucket = "MEDIUM"
        elif minutes <= 10080:
            bucket = "LONG"
        else:
            bucket = "VERY_LONG"
        by_bc[(bucket, "any")].append(t)
    bucket_edge = _bucket_edge_score(by_bc)

    activity = _activity_score(record, len(recent_trades))

    penalty_total, penalty_notes = _detect_penalties(recent_trades)
    notes.extend(penalty_notes)

    # Raw weighted sum
    raw = (
        W_PRIOR * long_prior
        + W_EWMA_14D * max(0.0, min(1.0, 0.5 + ewma_14d))  # shift+clip ewma to [0,1]
        + W_COPY_EFF * max(0.0, min(1.0, copy_efficiency))
        + W_CONSISTENCY * consistency
        + W_BUCKET_EDGE * bucket_edge
        + W_ACTIVITY * activity
        - penalty_total
    )

    # Bayesian regularization on n_recent
    n_recent = len(recent_trades)
    if n_recent < MIN_RECENT_FOR_BOOST:
        regularized = BASE_PRIOR_NEUTRAL  # not enough data — neutral
        notes.append(f"low_sample(n={n_recent}<{MIN_RECENT_FOR_BOOST})")
    else:
        alpha = min(n_recent, N_RECENT_PRIOR_TRANSITION) / N_RECENT_PRIOR_TRANSITION
        regularized = alpha * raw + (1 - alpha) * BASE_PRIOR_NEUTRAL

    # Cap clamp
    final = max(cap_min, min(cap_max, regularized))
    if final != regularized:
        notes.append(f"clamped[{cap_min},{cap_max}]")

    return WalletWeight(
        wallet=wallet,
        n_recent=n_recent,
        long_prior=round(long_prior, 4),
        ewma_14d=round(ewma_14d, 6),
        copy_efficiency=round(copy_efficiency, 4),
        consistency=round(consistency, 4),
        bucket_edge=round(bucket_edge, 4),
        activity=round(activity, 4),
        penalties=round(penalty_total, 4),
        raw_weight=round(raw, 4),
        regularized_weight=round(regularized, 4),
        final_weight=round(final, 4),
        notes=notes,
    )


def compute_wallet_weights(
    session: Session,
    *,
    window_days: int = 14,
    paper_mode: bool = True,
) -> dict[str, WalletWeight]:
    """Compute per-wallet weight for all wallets having recent trades.

    Returns dict[wallet_address → WalletWeight]. Pure function — only reads
    DB. Caller decides how to use weights (slot priority, sizing, etc.).
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    cap_min = PAPER_CAP_MIN if paper_mode else LIVE_INITIAL_CAP_MIN
    cap_max = PAPER_CAP_MAX if paper_mode else LIVE_INITIAL_CAP_MAX

    # Group recent closed trades by wallet
    by_wallet: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in session.exec(select(PaperTrade).where(PaperTrade.status == "closed")).all():
        if not t.wallet_address:
            continue
        closed = _ensure_aware(t.closed_at)
        if closed is None or closed < cutoff:
            continue
        by_wallet[t.wallet_address.lower()].append(t)

    # Pre-fetch copy efficiency per wallet (batch from M1 engine)
    try:
        from app.services.copy_efficiency_engine import (
            compute_copy_efficiency_report,
        )
        ce_report = compute_copy_efficiency_report(
            session, window_hours=window_days * 24
        )
        copy_eff_by_wallet: dict[str, float] = {
            w["wallet"]: w["ratio"] for w in ce_report.by_wallet_top + ce_report.by_wallet_bottom
        }
    except Exception:
        copy_eff_by_wallet = {}

    # Pre-fetch wallet records for long_prior + activity_score
    wallet_records: dict[str, MarketFirstWalletRecord] = {}
    for w in by_wallet:
        try:
            rec = session.exec(
                select(MarketFirstWalletRecord).where(
                    MarketFirstWalletRecord.address == w
                )
            ).first()
            if rec:
                wallet_records[w] = rec
        except Exception:
            pass

    weights: dict[str, WalletWeight] = {}
    for wallet, trades in by_wallet.items():
        ce = copy_eff_by_wallet.get(wallet, 0.5)  # neutral default
        ww = _compute_one_wallet_weight(
            wallet=wallet,
            record=wallet_records.get(wallet),
            recent_trades=trades,
            copy_efficiency=ce,
            cap_min=cap_min,
            cap_max=cap_max,
        )
        weights[wallet] = ww
    return weights


def wallet_weights_payload(
    session: Session, *, window_days: int = 14, paper_mode: bool = True,
) -> dict[str, Any]:
    weights = compute_wallet_weights(
        session, window_days=window_days, paper_mode=paper_mode
    )
    distribution = sorted(
        [w.final_weight for w in weights.values()]
    )
    n = len(distribution)
    return {
        "n_wallets": n,
        "window_days": window_days,
        "paper_mode": paper_mode,
        "weight_distribution": {
            "min": distribution[0] if n else None,
            "p25": distribution[n // 4] if n >= 4 else None,
            "median": distribution[n // 2] if n else None,
            "p75": distribution[3 * n // 4] if n >= 4 else None,
            "max": distribution[-1] if n else None,
            "mean": (sum(distribution) / n) if n else None,
        },
        "wallets": {w: asdict(ww) for w, ww in weights.items()},
    }
