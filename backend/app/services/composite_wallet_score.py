"""v0.5.6 Phase A — CompositeWalletScore.

Continuous score 0-100 derived from nine sub-scores. The composite
drives tier classification together with the hard gates encoded by
:func:`apply_tier_rules`.

Weight allocation (sum = 100 %):

* 20 %  EV_lower_bound          — bootstrap p20 of mean per-trade return
* 15 %  sample_size             — log-scaled, plateau at sample ≥ 1000
* 15 %  persistence_score       — fraction of usable quartiles with EV>0
* 15 %  activity_recency        — linear decay 1.0 → 0.0 between 2 and 26 weeks
* 10 %  validation               — val_wr × (1 − val_gap), 0..1
* 10 %  category_specialization  — Herfindahl × fraction of positive-EV cats
*  5 %  position_size_sanity     — binary 100/0
*  5 %  regime_robustness        — fraction of usable eras with positive EV
*  5 %  liquidity_copyability    — placeholder 50 (Phase B copyable edge)

Each sub-score is normalised to 0..100. The composite is also 0..100.

The ELITE/STRONG/CANDIDATE_EDGE/WATCH/IGNORE/SUSPICIOUS tier is
returned by :func:`apply_tier_rules`, which combines the composite with
hard gates from :class:`WalletEdgeMetrics`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.services.edge_quality_engine import (
    BLOCK_ANY_TIER_STATUSES,
    BLOCK_ELITE_ONLY_STATUSES,
    WalletEdgeMetrics,
)


WEIGHTS: dict[str, float] = {
    "ev_lower_bound": 0.20,
    "sample_size": 0.15,
    "persistence": 0.15,
    "activity_recency": 0.15,
    "validation": 0.10,
    "category_specialization": 0.10,
    "position_size_sanity": 0.05,
    "regime_robustness": 0.05,
    "liquidity_copyability": 0.05,
}


@dataclass
class CompositeBreakdown:
    address: str
    composite: float
    sub_scores: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "composite": self.composite,
            "sub_scores": dict(self.sub_scores),
            "weights": dict(self.weights),
            "notes": list(self.notes),
        }


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _ev_lower_bound_score(ev_lb: float | None) -> float:
    """Sigmoid-like mapping centred on 0:
    ev_lb = -0.20 → 0
    ev_lb = -0.05 →  ≈ 25
    ev_lb =  0.00 → 50
    ev_lb =  0.05 →  ≈ 75
    ev_lb =  0.20 → 100
    """
    if ev_lb is None:
        return 0.0
    # Linear map clamped to [-0.20, 0.20] then rescaled to 0..100.
    if ev_lb <= -0.20:
        return 0.0
    if ev_lb >= 0.20:
        return 100.0
    return _clamp(50.0 + (ev_lb / 0.20) * 50.0)


def _sample_size_score(sample: int | None) -> float:
    if not sample:
        return 0.0
    if sample <= 1:
        return 0.0
    # log10 scale: 30 → 50, 100 → 67, 300 → 80, 1000 → 100, plateau above.
    return _clamp((math.log10(min(sample, 1000)) / math.log10(1000)) * 100.0)


def _persistence_score(persistence: float | None) -> float:
    if persistence is None:
        return 0.0
    return _clamp(persistence * 100.0)


def _activity_recency_score(score: float | None) -> float:
    if score is None:
        return 0.0
    return _clamp(score * 100.0)


def _validation_score(
    validation_wr: float | None,
    validation_gap: float | None,
) -> float:
    if validation_wr is None:
        return 0.0
    if validation_gap is None:
        # Treat "unknown gap" as worst-case for ELITE prudence.
        return _clamp(validation_wr * 60.0)
    factor = max(0.0, 1.0 - min(1.0, validation_gap))
    return _clamp(validation_wr * factor * 100.0)


def _category_specialization_score(category_edge_map: dict[str, dict[str, Any]]) -> float:
    if not category_edge_map:
        return 0.0
    samples = [(cat, info.get("sample") or 0, info.get("ev")) for cat, info in category_edge_map.items()]
    total = sum(s[1] for s in samples)
    if total == 0:
        return 0.0
    # Herfindahl (concentration) — high when expertise is focused, capped at 1.
    herf = sum((s[1] / total) ** 2 for s in samples)
    # Fraction of cats (sample-weighted) with positive EV.
    pos_share = sum(s[1] for s in samples if (s[2] or 0) > 0) / total
    return _clamp((0.5 * herf + 0.5 * pos_share) * 100.0)


def _position_size_sanity_score(payload: dict[str, Any]) -> float:
    return 100.0 if payload.get("is_sane") else 0.0


def _regime_robustness_score(payload: dict[str, Any]) -> float:
    fraction = payload.get("fraction_positive")
    if fraction is None:
        return 50.0  # neutral when no era data
    return _clamp(fraction * 100.0)


def _liquidity_copyability_score(_metrics: WalletEdgeMetrics) -> float:
    # Phase A placeholder — refined in Phase B with copyable edge engine.
    return 50.0


def compute(
    metrics: WalletEdgeMetrics,
    *,
    validation_win_rate: float | None = None,
    validation_gap: float | None = None,
) -> CompositeBreakdown:
    """Aggregate the nine sub-scores into a composite 0..100.

    ``validation_win_rate`` and ``validation_gap`` come from the OOS
    validation report (CandidateValidationService). Pass ``None`` if
    the wallet was never OOS-tested — the validation sub-score collapses
    to zero in that case.
    """
    sub: dict[str, float] = {
        "ev_lower_bound": _ev_lower_bound_score(metrics.ev_lower_bound),
        "sample_size": _sample_size_score(metrics.sample_size),
        "persistence": _persistence_score(metrics.persistence_score),
        "activity_recency": _activity_recency_score(metrics.activity_recency_score),
        "validation": _validation_score(validation_win_rate, validation_gap),
        "category_specialization": _category_specialization_score(metrics.category_edge_map),
        "position_size_sanity": _position_size_sanity_score(metrics.position_size_sanity),
        "regime_robustness": _regime_robustness_score(metrics.regime_robustness),
        "liquidity_copyability": _liquidity_copyability_score(metrics),
    }
    composite = sum(sub[k] * WEIGHTS[k] for k in WEIGHTS)
    return CompositeBreakdown(
        address=metrics.address,
        composite=round(composite, 2),
        sub_scores={k: round(v, 2) for k, v in sub.items()},
        weights=dict(WEIGHTS),
    )


# ----------------- tier classification -----------------

ELITE_RULES = {
    "min_composite": 75.0,        # empirical baseline; refined by ev_distribution_report
    "min_sample": 100,
    "required_confidence": "HIGH",
    "min_ev_lower_bound": 0.01,   # > 0 with margin
    "min_validation_win_rate": 0.70,
    "max_validation_gap": 0.15,
    "min_persistence": 0.70,
    "require_holdout_pass": True,
    "require_category_edge_map": True,
    "blocking_statuses": BLOCK_ANY_TIER_STATUSES | BLOCK_ELITE_ONLY_STATUSES,
}

STRONG_RULES = {
    "min_composite": 60.0,
    "min_sample": 30,
    "min_ev_lower_bound": 0.0,    # ≥ 0
    "min_validation_win_rate": 0.65,
    "max_validation_gap": 0.20,
    "blocking_statuses": BLOCK_ANY_TIER_STATUSES,
}

CANDIDATE_EDGE_RULES = {
    "min_composite": 45.0,
    "min_ev_point": 0.005,
    "min_ev_lower_bound": -0.05,  # close to zero, maybe with insufficient sample
}


@dataclass
class TierDecision:
    address: str
    tier: str  # ELITE / STRONG / CANDIDATE_EDGE / WATCH / IGNORE / SUSPICIOUS
    composite: float
    rules_passed: list[str] = field(default_factory=list)
    rules_failed: list[str] = field(default_factory=list)
    blocking_statuses: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "tier": self.tier,
            "composite": self.composite,
            "rules_passed": list(self.rules_passed),
            "rules_failed": list(self.rules_failed),
            "blocking_statuses": list(self.blocking_statuses),
        }


def apply_tier_rules(
    metrics: WalletEdgeMetrics,
    breakdown: CompositeBreakdown,
    *,
    validation_win_rate: float | None = None,
    validation_gap: float | None = None,
    elite_min_composite: float | None = None,
    elite_min_ev_lower_bound: float | None = None,
    strong_min_composite: float | None = None,
    strong_min_ev_lower_bound: float | None = None,
) -> TierDecision:
    """Hard-gate tier classification.

    Threshold overrides (``elite_min_composite``, ``strong_min_composite``,
    etc.) let the driver inject empirically-derived percentile cutoffs
    from the EV distribution report instead of the static defaults.
    """
    elite_composite_threshold = elite_min_composite if elite_min_composite is not None else ELITE_RULES["min_composite"]
    elite_ev_lb_threshold = (
        elite_min_ev_lower_bound if elite_min_ev_lower_bound is not None else ELITE_RULES["min_ev_lower_bound"]
    )
    strong_composite_threshold = (
        strong_min_composite if strong_min_composite is not None else STRONG_RULES["min_composite"]
    )
    strong_ev_lb_threshold = (
        strong_min_ev_lower_bound if strong_min_ev_lower_bound is not None else STRONG_RULES["min_ev_lower_bound"]
    )

    blocking = [s for s in metrics.exclusion_status if s in (BLOCK_ANY_TIER_STATUSES | BLOCK_ELITE_ONLY_STATUSES)]
    suspicious_pattern = "BAD_ENTRY_PRICE_PATTERN" in metrics.exclusion_status or "OVERFIT_RISK" in metrics.exclusion_status

    # Tier ELITE? evaluate every rule.
    elite_passed: list[str] = []
    elite_failed: list[str] = []

    def _check(passed: bool, name: str) -> None:
        (elite_passed if passed else elite_failed).append(name)

    _check(breakdown.composite >= elite_composite_threshold, f"composite≥{elite_composite_threshold:.0f}")
    _check(metrics.sample_size >= ELITE_RULES["min_sample"], f"sample≥{ELITE_RULES['min_sample']}")
    _check(metrics.ev_confidence == ELITE_RULES["required_confidence"], "confidence=HIGH")
    _check(
        metrics.ev_lower_bound is not None and metrics.ev_lower_bound >= elite_ev_lb_threshold,
        f"ev_lb≥{elite_ev_lb_threshold:.3f}",
    )
    _check(
        validation_win_rate is not None and validation_win_rate >= ELITE_RULES["min_validation_win_rate"],
        f"val_wr≥{ELITE_RULES['min_validation_win_rate']:.2f}",
    )
    _check(
        validation_gap is not None and validation_gap <= ELITE_RULES["max_validation_gap"],
        f"val_gap≤{ELITE_RULES['max_validation_gap']:.2f}",
    )
    _check(
        metrics.persistence_score >= ELITE_RULES["min_persistence"],
        f"persistence≥{ELITE_RULES['min_persistence']:.2f}",
    )
    _check(metrics.holdout_pass, "holdout_pass")
    _check(bool(metrics.category_edge_map), "category_edge_map_non_empty")
    _check(not blocking, "no_blocking_status")

    if not elite_failed:
        return TierDecision(
            address=metrics.address,
            tier="ELITE",
            composite=breakdown.composite,
            rules_passed=elite_passed,
            rules_failed=[],
            blocking_statuses=blocking,
        )

    # Demote: STRONG check.
    strong_passed: list[str] = []
    strong_failed: list[str] = []
    has_blocking_any = any(s in BLOCK_ANY_TIER_STATUSES for s in blocking)

    def _scheck(passed: bool, name: str) -> None:
        (strong_passed if passed else strong_failed).append(name)

    _scheck(breakdown.composite >= strong_composite_threshold, f"composite≥{strong_composite_threshold:.0f}")
    _scheck(metrics.sample_size >= STRONG_RULES["min_sample"], f"sample≥{STRONG_RULES['min_sample']}")
    _scheck(
        metrics.ev_lower_bound is not None and metrics.ev_lower_bound >= strong_ev_lb_threshold,
        f"ev_lb≥{strong_ev_lb_threshold:.3f}",
    )
    if validation_win_rate is not None:
        _scheck(
            validation_win_rate >= STRONG_RULES["min_validation_win_rate"],
            f"val_wr≥{STRONG_RULES['min_validation_win_rate']:.2f}",
        )
    if validation_gap is not None:
        _scheck(
            validation_gap <= STRONG_RULES["max_validation_gap"],
            f"val_gap≤{STRONG_RULES['max_validation_gap']:.2f}",
        )
    _scheck(not has_blocking_any, "no_block_any")

    if not strong_failed:
        return TierDecision(
            address=metrics.address,
            tier="STRONG",
            composite=breakdown.composite,
            rules_passed=strong_passed,
            rules_failed=elite_failed,
            blocking_statuses=blocking,
        )

    # Suspicious patterns → SUSPICIOUS regardless of composite.
    if suspicious_pattern:
        return TierDecision(
            address=metrics.address,
            tier="SUSPICIOUS",
            composite=breakdown.composite,
            rules_passed=[],
            rules_failed=elite_failed + strong_failed,
            blocking_statuses=blocking,
        )

    # CANDIDATE_EDGE: positive EV point + ev_lb close to zero, sample insufficient.
    if (
        metrics.expected_value is not None
        and metrics.expected_value >= CANDIDATE_EDGE_RULES["min_ev_point"]
        and (metrics.ev_lower_bound or -1.0) >= CANDIDATE_EDGE_RULES["min_ev_lower_bound"]
        and breakdown.composite >= CANDIDATE_EDGE_RULES["min_composite"]
    ):
        return TierDecision(
            address=metrics.address,
            tier="CANDIDATE_EDGE",
            composite=breakdown.composite,
            rules_passed=[],
            rules_failed=elite_failed + strong_failed,
            blocking_statuses=blocking,
        )

    # WATCH: positive ev_point even without other gates passing.
    if metrics.expected_value is not None and metrics.expected_value > 0 and not has_blocking_any:
        return TierDecision(
            address=metrics.address,
            tier="WATCH",
            composite=breakdown.composite,
            rules_passed=[],
            rules_failed=elite_failed + strong_failed,
            blocking_statuses=blocking,
        )

    return TierDecision(
        address=metrics.address,
        tier="IGNORE",
        composite=breakdown.composite,
        rules_passed=[],
        rules_failed=elite_failed + strong_failed,
        blocking_statuses=blocking,
    )
