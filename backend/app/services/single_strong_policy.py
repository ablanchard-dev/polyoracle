"""v0.7.8 Priority 3 (C encadré) — single STRONG strict trigger policy.

Behind a feature flag. Single STRONG wallet can trigger a cluster ONLY
if ALL of the following 10 gates pass simultaneously:

1. policy enabled (env var)
2. wallet_tier == STRONG (ELITE keeps default behaviour)
3. capital <= SINGLE_STRONG_STRICT_MAX_CAPITAL_EUR (default 150€)
4. market_speed_class in {FAST}
5. duration_bucket in {ULTRA_SHORT, VERY_SHORT}
6. category known (not None / "Unknown")
7. deadline known (expected_holding_time / market.deadline)
8. copyable_edge >= 0.30
9. ev_after_fee > 0.01
10. spread reasonable (< max_spread_pct)
11. orderbook_quality not in {UNTRADABLE, BAD, INSUFFICIENT_DATA}
12. wallet not in suspicious / blocked tier sets

If ANY gate fails -> standard cluster behaviour (PENDING / require 2-wallet
consensus). If ALL pass -> override to TRIGGER, with explicit
classification = SINGLE_STRONG_STRICT_TRIGGER for traceability.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


SINGLE_STRONG_STRICT_POLICY_ENABLED = os.environ.get(
    "SINGLE_STRONG_STRICT_POLICY", "0"
) == "1"

SINGLE_STRONG_STRICT_MAX_CAPITAL_EUR = float(
    os.environ.get("SINGLE_STRONG_STRICT_MAX_CAPITAL_EUR", "150")
)

SINGLE_STRONG_STRICT_MIN_EDGE = float(
    os.environ.get("SINGLE_STRONG_STRICT_MIN_EDGE", "0.30")
)

SINGLE_STRONG_STRICT_MIN_EV_AFTER_FEE = float(
    os.environ.get("SINGLE_STRONG_STRICT_MIN_EV_AFTER_FEE", "0.01")
)

SINGLE_STRONG_STRICT_MAX_SPREAD = float(
    os.environ.get("SINGLE_STRONG_STRICT_MAX_SPREAD", "0.04")
)


@dataclass
class SingleStrongStrictContext:
    """All inputs to the eligibility check, packed into one struct so the
    policy is fully self-contained and testable."""

    wallet_tier: str
    capital_total_eur: float
    market_speed_class: str | None
    duration_bucket: str | None
    category: str | None
    deadline_known: bool
    copyable_edge: float
    ev_after_fee: float
    spread: float
    orderbook_quality: str | None


_SUSPICIOUS_TIERS = frozenset({
    "BLOCKED", "SUSPICIOUS", "IGNORE", "WEAK", "DROPPED",
    "OUTLIER_FLAGGED", "FAILED_VALIDATION", "BIASED_SAMPLE",
})

_BAD_ORDERBOOK = frozenset({"UNTRADABLE", "BAD", "INSUFFICIENT_DATA"})

_FAST_SPEED_CLASSES = frozenset({"FAST"})
_SHORT_DURATION_BUCKETS = frozenset({"ULTRA_SHORT", "VERY_SHORT"})


def is_single_strong_strict_eligible(ctx: SingleStrongStrictContext) -> tuple[bool, str | None]:
    """Return (eligible, fail_reason).

    fail_reason is the FIRST gate that failed (None if all pass).
    """
    if not SINGLE_STRONG_STRICT_POLICY_ENABLED:
        return False, "policy_disabled"
    tier = (ctx.wallet_tier or "").strip().upper()
    if tier == "ELITE":
        # ELITE goes through default behaviour (already triggers alone via B7-A)
        return False, "tier_is_elite_default_path"
    if tier != "STRONG":
        return False, f"tier_not_strong:{tier}"
    if tier in _SUSPICIOUS_TIERS:
        return False, "tier_suspicious"
    if ctx.capital_total_eur > SINGLE_STRONG_STRICT_MAX_CAPITAL_EUR:
        return False, f"capital_above_max:{ctx.capital_total_eur}"
    speed = (ctx.market_speed_class or "").strip().upper()
    if speed not in _FAST_SPEED_CLASSES:
        return False, f"speed_not_fast:{speed}"
    bucket = (ctx.duration_bucket or "").strip().upper()
    if bucket not in _SHORT_DURATION_BUCKETS:
        return False, f"duration_not_short:{bucket}"
    cat = (ctx.category or "").strip()
    if not cat or cat.lower() == "unknown":
        return False, "category_unknown"
    if not ctx.deadline_known:
        return False, "deadline_unknown"
    if ctx.copyable_edge < SINGLE_STRONG_STRICT_MIN_EDGE:
        return False, f"edge_below_min:{ctx.copyable_edge}"
    if ctx.ev_after_fee < SINGLE_STRONG_STRICT_MIN_EV_AFTER_FEE:
        return False, f"ev_after_fee_below_min:{ctx.ev_after_fee}"
    if ctx.spread > SINGLE_STRONG_STRICT_MAX_SPREAD:
        return False, f"spread_too_wide:{ctx.spread}"
    ob = (ctx.orderbook_quality or "").strip().upper()
    if ob in _BAD_ORDERBOOK:
        return False, f"orderbook_bad:{ob}"
    return True, None
