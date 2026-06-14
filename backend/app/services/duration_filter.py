"""v0.7.6 B13 — capital-aware duration filter.

Markets have different expected resolution horizons. With small capital,
locking $50 for 7 days at +5% EV is much worse than locking it for 5 min
at +1% EV (much higher capital turnover). This module exposes 6 explicit
duration buckets and a capital-aware filter so the allocator can reject
or down-weight long-locks when capital is tight.
"""

from __future__ import annotations

from typing import Optional

# (lower_bound_min, upper_bound_min) inclusive-exclusive in minutes.
DURATION_BUCKETS: dict[str, tuple[int, int | None]] = {
    "ULTRA_SHORT": (0, 5),
    "VERY_SHORT": (5, 30),
    "SHORT": (30, 120),
    "MEDIUM": (120, 1440),
    "LONG": (1440, 10080),
    "VERY_LONG": (10080, None),
}


def classify_duration(expected_resolution_minutes: float) -> str:
    """Return one of the 6 buckets for a given expected resolution time.

    None / negative resolution defaults to ``VERY_LONG`` (= worst case;
    we cannot promise rotation)."""
    if expected_resolution_minutes is None or expected_resolution_minutes < 0:
        return "VERY_LONG"
    minutes = float(expected_resolution_minutes)
    for bucket, (lo, hi) in DURATION_BUCKETS.items():
        if hi is None:
            if minutes >= lo:
                return bucket
        elif lo <= minutes < hi:
            return bucket
    return "VERY_LONG"


def compute_ev_per_hour(ev_after_fee: float, expected_resolution_minutes: float) -> float:
    """EV per hour of capital lock. Floor minutes at 5 min so very short
    durations don't blow up to infinity."""
    minutes = max(float(expected_resolution_minutes or 5.0), 5.0)
    hours = minutes / 60.0
    return float(ev_after_fee) / hours


def compute_capital_lock_penalty(
    expected_resolution_minutes: float, capital_total: float
) -> float:
    """Penalty in [0, 1] for locking capital. Larger capital = more
    forgiving. Spec values:

    - capital >= 5000 -> 0 (no penalty, plenty of room)
    - 1000-5000 -> normalized over 24h
    - 500-1000 -> normalized over 12h
    - 100-500 -> normalized over 6h
    - <= 50 -> normalized over 1h
    """
    minutes = float(expected_resolution_minutes or 0)
    cap = float(capital_total or 0)
    if cap >= 5000.0:
        return 0.0
    if cap >= 1000.0:
        return min(1.0, minutes / 1440.0)
    if cap >= 500.0:
        return min(1.0, minutes / 720.0)
    if cap >= 100.0:
        return min(1.0, minutes / 360.0)
    # <= 50 (or <100) — strict
    return min(1.0, minutes / 60.0)


def compute_capital_turnover_score(
    ev_per_hour: float, capital_lock_penalty: float
) -> float:
    """0-100 score combining EV/hour reward with the lock penalty.

    score = clamp(ev_per_hour * 1000 - capital_lock_penalty * 50, 0, 100)
    """
    raw = float(ev_per_hour) * 1000.0 - float(capital_lock_penalty) * 50.0
    return max(0.0, min(100.0, raw))


# v0.7.8 P6 — capital-aware duration tiers, graduated per operator spec.
#
# Operator rule: "à 100€ le bot doit finir ses trades vite, à mesure que
# le capital monte on peut allouer une part à des trades >10 min".
#
# So the duration filter is GRADUATED:
#   <  500 USD  → SMALL  : SHORT-ONLY (1-10 min), >10 min hard reject
#   < 5000      → MEDIUM : 1-30 min, >30 edge-conditional, >120 reject
#   < 50000     → LARGE  : 1-120 min, >120 edge-conditional, >7d reject
#   ≥ 50000     → HUGE   : balanced, only VERY_LONG (>7d) rejected unless edge

_CAPITAL_TIER_SMALL_MAX: float = 500.0     # < $500 → SHORT-ONLY
_CAPITAL_TIER_MEDIUM_MAX: float = 5_000.0  # < $5k → mostly short
_CAPITAL_TIER_LARGE_MAX: float = 50_000.0  # < $50k → balanced


def _exceptional_override(
    ev_lower_bound: Optional[float],
    capital_turnover_score: Optional[float],
    *,
    min_ev: float,
    min_score: float,
) -> bool:
    """An override only fires when both EV and turnover clear the bar."""
    ev_ok = ev_lower_bound is not None and ev_lower_bound >= min_ev
    sc_ok = capital_turnover_score is not None and capital_turnover_score >= min_score
    return ev_ok and sc_ok


def filter_duration_for_capital(
    capital_total: float,
    duration_bucket: str,
    *,
    expected_minutes: Optional[float] = None,
    ev_lower_bound: Optional[float] = None,
    capital_turnover_score: Optional[float] = None,
) -> tuple[bool, Optional[str]]:
    """Return ``(allowed, reject_reason)``.

    v0.7.8 P6 final — graduated per capital tier:

    - **SMALL (< $500)**: SHORT-ONLY. Operator rule "à 100€ le bot doit
      finir ses trades vite". 1-5 min auto-allow. 5-10 min edge-conditional
      (need ev≥0.05 AND turnover≥15). >10 min hard reject.
    - **MEDIUM (< $5000)**: short-mostly. 1-30 min auto-allow. 30-120 min
      edge-conditional. 120-1440 min strong exception only. >24h reject.
    - **LARGE (< $50000)**: 1-1440 min (≤24h) auto-allow. LONG/VERY_LONG
      hard reject (operator rule 2026-05-16 : "avant 50k minimum, aucun
      trade >24h"). No edge override at this tier for long duration.
    - **HUGE (≥ $50000)**: institutional. All buckets allowed except
      VERY_LONG (>7 days) which needs strong edge.

    The VERY_SHORT bucket (5-30 min) at SMALL is split by actual minutes:
    5-10 min edge-conditional, 10-30 min reject. ``expected_minutes``
    must be passed for this fine-grained split — legacy callers without
    it get the conservative reject for VERY_SHORT >5min at SMALL.
    """
    cap = float(capital_total or 0)
    minutes = float(expected_minutes) if expected_minutes is not None else None

    # ====================================================================
    # SMALL (< $500) — SHORT-ONLY: 1-5 prio, 5-10 edge, >10 reject
    # ====================================================================
    if cap < _CAPITAL_TIER_SMALL_MAX:
        # Reverted 2026-06 — the abandoned live-v2 merge (d1d7e05, "duration_filter
        # 15min autorisé à SMALL") blanket-allowed VERY_SHORT up to 30 min at SMALL,
        # dropping the fine-grained 10-min split and breaking the "small capital must
        # rotate fast" guardrail. Restored to the conservative split below (matches
        # the docstring and the B13 spec tests). Re-apply the loosening deliberately
        # only if the live push is resumed and the upstream band gate is in place.
        if duration_bucket == "ULTRA_SHORT":  # 0-5 min — auto-allow
            return True, None
        if duration_bucket == "VERY_SHORT":   # 5-30 min — split @ 10 min
            if minutes is None:
                # Conservative default: reject (no minutes → can't be sure
                # it's ≤10 min). Production callers MUST pass minutes.
                return False, "CAPITAL_LOCK_TOO_LONG"
            if minutes <= 10.0:
                # 5-10 min — edge-conditional
                if _exceptional_override(
                    ev_lower_bound, capital_turnover_score,
                    min_ev=0.05, min_score=15.0,
                ):
                    return True, None
                return False, "CAPITAL_LOCK_TOO_LONG"
            # 10-30 min — reject at SMALL (locks growth-phase capital)
            return False, "CAPITAL_LOCK_TOO_LONG"
        # SHORT (30-120) and beyond — reject at SMALL no exception
        return False, "CAPITAL_LOCK_TOO_LONG"

    # ====================================================================
    # MEDIUM ($500-5000) — short-mostly: 1-30 auto, 30-120 edge, 120-1440 strong
    # ====================================================================
    if cap < _CAPITAL_TIER_MEDIUM_MAX:
        if duration_bucket in ("ULTRA_SHORT", "VERY_SHORT"):
            return True, None
        if duration_bucket == "SHORT":  # 30-120 min — edge-conditional
            if _exceptional_override(
                ev_lower_bound, capital_turnover_score,
                min_ev=0.10, min_score=20.0,
            ):
                return True, None
            return False, "CAPITAL_LOCK_TOO_LONG"
        if duration_bucket == "MEDIUM":  # 120-1440 min — strong exception
            if _exceptional_override(
                ev_lower_bound, capital_turnover_score,
                min_ev=0.20, min_score=30.0,
            ):
                return True, None
            return False, "CAPITAL_LOCK_TOO_LONG"
        # LONG, VERY_LONG — reject at MEDIUM
        return False, "CAPITAL_LOCK_TOO_LONG"

    # ====================================================================
    # LARGE ($5k-50k) — balanced: 1-1440 auto, 1-7d edge, >7d reject
    # ====================================================================
    # Operator rule 2026-05-16 : "avant 50k minimum, aucun trade >24h".
    # LARGE ($5k-50k) now caps at MEDIUM bucket (≤24h). LONG / VERY_LONG
    # hard reject, no edge override.
    if cap < _CAPITAL_TIER_LARGE_MAX:
        if duration_bucket in ("ULTRA_SHORT", "VERY_SHORT", "SHORT", "MEDIUM"):
            return True, None
        return False, "CAPITAL_LOCK_TOO_LONG"

    # ====================================================================
    # HUGE (≥$50k) — institutional: all but VERY_LONG (which needs edge)
    # ====================================================================
    if duration_bucket == "VERY_LONG":
        if _exceptional_override(
            ev_lower_bound, capital_turnover_score,
            min_ev=0.10, min_score=30.0,
        ):
            return True, None
        return False, "CAPITAL_LOCK_TOO_LONG"
    return True, None
