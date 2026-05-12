"""Trade quality score (Phase A complement — 2026-05-11).

Two scores per trade :

  pre_trade_score (0-100) — computed BEFORE opening, combines :
    wallet_score, copy_efficiency_du_wallet, freshness_signal,
    spread_score, liquidity_score, copyable_edge,
    expected_R_to_R, duration_bucket_edge, category_score, cluster_confidence

  post_close_score (0-100) — computed AT resolution, combines :
    realized_R, slippage_actual_vs_expected, copy_delay_actual,
    fill_quality, source_wallet_realized_R sur le même trade

Comparing them per trade lets us calibrate pre_trade_score weights:
  if pre_trade_score consistently overestimates post_close_score, recalibrate.

Pure function — no DB writes. To be invoked from paper_trading_engine
(activation post-Phase B baseline). Phase A skeleton lets us test logic
independently first.

Used by:
  - Operator-facing dashboard (per-trade quality strip)
  - Phase C C1 weighting input feature
  - Phase D dashboard alerts (low score sustained = quality regression)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --------- Pre-trade weights (sum to 1.0) ---------

PRE_W_WALLET = 0.20
PRE_W_COPY_EFF = 0.15
PRE_W_FRESHNESS = 0.10
PRE_W_SPREAD = 0.10
PRE_W_LIQUIDITY = 0.10
PRE_W_COPYABLE_EDGE = 0.15
PRE_W_RR = 0.10
PRE_W_BUCKET = 0.05
PRE_W_CATEGORY = 0.03
PRE_W_CLUSTER = 0.02

PRE_OPEN_THRESHOLD = 60.0  # below = passed to WATCH (no open) even if gates ok

# --------- Post-close weights ---------

POST_W_REALIZED_R = 0.30
POST_W_SLIPPAGE_FIT = 0.20
POST_W_COPY_DELAY = 0.15
POST_W_FILL_QUALITY = 0.15
POST_W_SOURCE_R_RATIO = 0.20


@dataclass
class PreTradeInputs:
    wallet_score: float = 0.0  # 0-100
    copy_efficiency_wallet: float = 0.5  # ratio
    freshness_seconds: float = 0.0  # signal lag
    spread: float = 0.0
    liquidity_score: float = 0.0  # 0-100
    copyable_edge: float = 0.0  # 0-100
    expected_rr: float = 1.0  # reward/risk ratio
    duration_bucket: str = "UNKNOWN"
    category: str = "unknown"
    cluster_confidence: float = 0.0  # 0-1


@dataclass
class PostCloseInputs:
    realized_r: float = 0.0
    expected_slippage: float = 0.0
    actual_slippage: float = 0.0
    expected_copy_delay_s: float = 0.0
    actual_copy_delay_s: float = 0.0
    fill_quality_pct: float = 1.0  # 0-1, % at quoted_or_better
    bot_realized_r: float = 0.0
    source_wallet_realized_r: float = 0.0


@dataclass
class TradeQualityResult:
    pre_trade_score: float
    pre_trade_breakdown: dict[str, float]
    post_close_score: float | None
    post_close_breakdown: dict[str, float]
    pre_post_gap: float | None  # post - pre. Negative = pre overestimated
    open_recommendation: str  # "OPEN" | "WATCH"
    notes: list[str] = field(default_factory=list)


def _normalize(v: float, lo: float, hi: float) -> float:
    """Clamp + normalize to 0-1 then × 100."""
    if hi <= lo:
        return 50.0
    n = (v - lo) / (hi - lo)
    return max(0.0, min(100.0, n * 100))


def _freshness_score(seconds: float) -> float:
    """Lower is better. 0s = 100, 30s = 80, 60s = 60, 120s = 40, 300s+ = 0."""
    if seconds <= 0:
        return 100.0
    if seconds >= 300:
        return 0.0
    return max(0.0, 100.0 - (seconds / 3.0))


def _spread_score(spread: float) -> float:
    """0.01 spread = 90, 0.03 = 60, 0.06 = 30, ≥0.10 = 0."""
    if spread <= 0.01:
        return 100.0
    if spread >= 0.10:
        return 0.0
    return max(0.0, 100.0 - (spread - 0.01) * 1000.0)


def _bucket_score(bucket: str) -> float:
    """Higher for shorter buckets (more capital rotation, NANO-friendly)."""
    return {
        "ULTRA_SHORT": 90.0,
        "VERY_SHORT": 80.0,
        "SHORT": 60.0,
        "MEDIUM": 40.0,
        "LONG": 20.0,
        "VERY_LONG": 10.0,
    }.get(bucket, 50.0)


def _category_score(category: str) -> float:
    """Higher for less competitive / better-known edges."""
    return {
        "politics": 80.0,
        "sports": 75.0,
        "geopolitics": 75.0,
        "finance": 70.0,
        "weather": 65.0,
        "culture": 60.0,
        "crypto": 50.0,
        "unknown": 40.0,
    }.get(category.lower(), 50.0)


def compute_pre_trade_score(inputs: PreTradeInputs) -> tuple[float, dict[str, float]]:
    """Returns (score 0-100, breakdown dict). Pure."""
    s_wallet = max(0.0, min(100.0, inputs.wallet_score))
    s_copy = max(0.0, min(100.0, inputs.copy_efficiency_wallet * 100))
    s_fresh = _freshness_score(inputs.freshness_seconds)
    s_spread = _spread_score(inputs.spread)
    s_liq = max(0.0, min(100.0, inputs.liquidity_score))
    s_edge = max(0.0, min(100.0, inputs.copyable_edge))
    s_rr = max(0.0, min(100.0, inputs.expected_rr * 50.0))  # rr=2 → 100
    s_bucket = _bucket_score(inputs.duration_bucket)
    s_cat = _category_score(inputs.category)
    s_cluster = max(0.0, min(100.0, inputs.cluster_confidence * 100))

    breakdown = {
        "wallet": round(PRE_W_WALLET * s_wallet, 2),
        "copy_efficiency": round(PRE_W_COPY_EFF * s_copy, 2),
        "freshness": round(PRE_W_FRESHNESS * s_fresh, 2),
        "spread": round(PRE_W_SPREAD * s_spread, 2),
        "liquidity": round(PRE_W_LIQUIDITY * s_liq, 2),
        "copyable_edge": round(PRE_W_COPYABLE_EDGE * s_edge, 2),
        "rr": round(PRE_W_RR * s_rr, 2),
        "bucket": round(PRE_W_BUCKET * s_bucket, 2),
        "category": round(PRE_W_CATEGORY * s_cat, 2),
        "cluster": round(PRE_W_CLUSTER * s_cluster, 2),
    }
    score = sum(breakdown.values())
    return (round(score, 2), breakdown)


def compute_post_close_score(inputs: PostCloseInputs) -> tuple[float, dict[str, float]]:
    s_realized = _normalize(inputs.realized_r, -2.0, 2.0)
    if inputs.expected_slippage > 0:
        slippage_ratio = inputs.actual_slippage / inputs.expected_slippage
        s_slip_fit = max(0.0, 100.0 - abs(slippage_ratio - 1.0) * 50.0)
    else:
        s_slip_fit = 50.0
    if inputs.expected_copy_delay_s > 0:
        delay_ratio = inputs.actual_copy_delay_s / inputs.expected_copy_delay_s
        s_delay = max(0.0, 100.0 - abs(delay_ratio - 1.0) * 50.0)
    else:
        s_delay = 50.0
    s_fill = max(0.0, min(100.0, inputs.fill_quality_pct * 100))
    if inputs.source_wallet_realized_r != 0:
        ratio = inputs.bot_realized_r / inputs.source_wallet_realized_r
        s_source_ratio = max(0.0, min(100.0, (ratio + 0.0) * 100))
    else:
        s_source_ratio = 50.0

    breakdown = {
        "realized_r": round(POST_W_REALIZED_R * s_realized, 2),
        "slippage_fit": round(POST_W_SLIPPAGE_FIT * s_slip_fit, 2),
        "copy_delay": round(POST_W_COPY_DELAY * s_delay, 2),
        "fill_quality": round(POST_W_FILL_QUALITY * s_fill, 2),
        "source_r_ratio": round(POST_W_SOURCE_R_RATIO * s_source_ratio, 2),
    }
    score = sum(breakdown.values())
    return (round(score, 2), breakdown)


def evaluate_trade_quality(
    pre: PreTradeInputs, post: PostCloseInputs | None = None,
) -> TradeQualityResult:
    pre_score, pre_breakdown = compute_pre_trade_score(pre)
    notes: list[str] = []

    open_rec = "OPEN" if pre_score >= PRE_OPEN_THRESHOLD else "WATCH"
    if open_rec == "WATCH":
        notes.append(f"pre_trade_score {pre_score:.1f} < {PRE_OPEN_THRESHOLD} threshold")

    if post is None:
        return TradeQualityResult(
            pre_trade_score=pre_score,
            pre_trade_breakdown=pre_breakdown,
            post_close_score=None,
            post_close_breakdown={},
            pre_post_gap=None,
            open_recommendation=open_rec,
            notes=notes,
        )

    post_score, post_breakdown = compute_post_close_score(post)
    gap = post_score - pre_score
    if gap < -20:
        notes.append(f"pre overestimated (gap={gap:.1f})")
    elif gap > 20:
        notes.append(f"pre underestimated (gap=+{gap:.1f})")

    return TradeQualityResult(
        pre_trade_score=pre_score,
        pre_trade_breakdown=pre_breakdown,
        post_close_score=post_score,
        post_close_breakdown=post_breakdown,
        pre_post_gap=round(gap, 2),
        open_recommendation=open_rec,
        notes=notes,
    )


def quality_payload(pre: PreTradeInputs, post: PostCloseInputs | None = None) -> dict[str, Any]:
    return asdict(evaluate_trade_quality(pre, post))
