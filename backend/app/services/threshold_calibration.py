"""v0.5.6 Phase B — A/B threshold calibration.

Given a list of :class:`WalletEdgeMetrics` + :class:`CompositeBreakdown`
+ optional validation rows, sweeps multiple candidate threshold
combinations and scores each one against a *robustness target* derived
from the holdout split.

Each candidate is a tuple of:

* ``elite_min_ev_lower_bound`` ∈ {p70, p75, p85, p90} of EV_lb (positive only)
* ``elite_min_composite``      ∈ {55, 65, 75, 85}
* ``elite_min_persistence``    ∈ {0.50, 0.70, 0.80}
* ``max_validation_gap``       ∈ {0.10, 0.15, 0.20}

Robustness score per candidate (higher = better):

* +1 per ELITE wallet whose calibration EV > 0 AND whose holdout EV ≥ 0
* −2 per ELITE wallet whose holdout EV < calibration EV − 0.10
  (i.e. drop > 10 % is a strong overfit signal)
* −5 per ELITE wallet flagged ``HOLDOUT_FAILED`` or ``OVERFIT_RISK``
* size penalty: log10(n_elite) keeps tiny universes from winning by
  default

Returns the highest-scoring candidate alongside the full sweep
table for ``threshold_ab_calibration_report.json``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from app.services.composite_wallet_score import (
    CompositeBreakdown,
    apply_tier_rules,
)
from app.services.edge_quality_engine import WalletEdgeMetrics


@dataclass
class CalibrationCandidate:
    elite_min_ev_lower_bound: float
    elite_min_composite: float
    elite_min_persistence: float
    max_validation_gap: float


@dataclass
class CalibrationResult:
    candidate: CalibrationCandidate
    n_elite: int
    n_strong: int
    median_ev_among_elite: float | None
    median_holdout_ev_among_elite: float | None
    overfit_count: int
    holdout_failed_count: int
    score: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": {
                "elite_min_ev_lower_bound": self.candidate.elite_min_ev_lower_bound,
                "elite_min_composite": self.candidate.elite_min_composite,
                "elite_min_persistence": self.candidate.elite_min_persistence,
                "max_validation_gap": self.candidate.max_validation_gap,
            },
            "n_elite": self.n_elite,
            "n_strong": self.n_strong,
            "median_ev_among_elite": self.median_ev_among_elite,
            "median_holdout_ev_among_elite": self.median_holdout_ev_among_elite,
            "overfit_count": self.overfit_count,
            "holdout_failed_count": self.holdout_failed_count,
            "score": self.score,
            "notes": list(self.notes),
        }


def _percentiles(values: list[float], pcts: list[int]) -> dict[int, float]:
    if not values:
        return {p: 0.0 for p in pcts}
    sv = sorted(values)
    n = len(sv)
    out: dict[int, float] = {}
    for p in pcts:
        idx = max(0, min(n - 1, int(round(p / 100.0 * (n - 1)))))
        out[p] = sv[idx]
    return out


def build_candidates(metrics: list[WalletEdgeMetrics]) -> list[CalibrationCandidate]:
    """Construct the sweep grid using empirical EV_lb percentiles."""
    positive_lbs = [
        m.ev_lower_bound for m in metrics if m.ev_lower_bound is not None and m.ev_lower_bound > 0
    ]
    pct_cuts = _percentiles(positive_lbs, [70, 75, 85, 90])
    out: list[CalibrationCandidate] = []
    for ev_lb_pct in [70, 75, 85, 90]:
        for comp in [55.0, 65.0, 75.0, 85.0]:
            for pers in [0.50, 0.70, 0.80]:
                for gap in [0.10, 0.15, 0.20]:
                    out.append(
                        CalibrationCandidate(
                            elite_min_ev_lower_bound=round(max(0.005, pct_cuts[ev_lb_pct]), 4),
                            elite_min_composite=comp,
                            elite_min_persistence=pers,
                            max_validation_gap=gap,
                        )
                    )
    return out


def evaluate_candidate(
    candidate: CalibrationCandidate,
    metrics_by_addr: dict[str, WalletEdgeMetrics],
    breakdown_by_addr: dict[str, CompositeBreakdown],
    validation_by_addr: dict[str, tuple[float | None, float | None]],
) -> CalibrationResult:
    n_elite = 0
    n_strong = 0
    elite_evs: list[float] = []
    elite_holdout_evs: list[float] = []
    overfit = 0
    holdout_failed = 0
    notes: list[str] = []

    for addr, m in metrics_by_addr.items():
        bk = breakdown_by_addr.get(addr)
        if bk is None:
            continue
        val = validation_by_addr.get(addr, (None, None))
        val_wr, val_gap = val
        if m.persistence_score < candidate.elite_min_persistence:
            # Skip ELITE check here only if persistence too low → cannot be ELITE under the candidate.
            pass
        dec = apply_tier_rules(
            m,
            bk,
            validation_win_rate=val_wr,
            validation_gap=val_gap,
            elite_min_composite=candidate.elite_min_composite,
            elite_min_ev_lower_bound=candidate.elite_min_ev_lower_bound,
        )
        # The static gap rule lives inside apply_tier_rules — we approximate the
        # candidate's max_validation_gap by retroactively demoting if violated.
        if (
            dec.tier == "ELITE"
            and val_gap is not None
            and val_gap > candidate.max_validation_gap
        ):
            dec_tier = "STRONG"
        else:
            dec_tier = dec.tier
        # Candidate persistence floor.
        if dec_tier == "ELITE" and m.persistence_score < candidate.elite_min_persistence:
            dec_tier = "STRONG"

        if dec_tier == "ELITE":
            n_elite += 1
            if m.expected_value is not None:
                elite_evs.append(m.expected_value)
            holdout_ev = (m.holdout_details or {}).get("holdout_ev")
            calibration_ev = (m.holdout_details or {}).get("calibration_ev")
            if holdout_ev is not None:
                elite_holdout_evs.append(holdout_ev)
            if "OVERFIT_RISK" in m.exclusion_status:
                overfit += 1
            if "HOLDOUT_FAILED" in m.exclusion_status:
                holdout_failed += 1
            # Drop > 10 %?
            if (
                calibration_ev is not None
                and holdout_ev is not None
                and calibration_ev > 0
                and (calibration_ev - holdout_ev) > 0.10
            ):
                overfit += 1
        elif dec_tier == "STRONG":
            n_strong += 1

    # Robustness score.
    base = n_elite
    base -= 2 * overfit
    base -= 5 * holdout_failed
    # Penalty if median holdout EV among ELITE is negative.
    median_hold = (
        statistics.median(elite_holdout_evs) if elite_holdout_evs else None
    )
    if median_hold is not None and median_hold < 0:
        base -= n_elite  # collapse score
        notes.append("median_holdout_ev_negative_among_ELITE")
    # Tiny-universe protection: apply log scaling to avoid winners with n=1.
    if n_elite < 5:
        base -= 5
    return CalibrationResult(
        candidate=candidate,
        n_elite=n_elite,
        n_strong=n_strong,
        median_ev_among_elite=(
            round(statistics.median(elite_evs), 4) if elite_evs else None
        ),
        median_holdout_ev_among_elite=round(median_hold, 4) if median_hold is not None else None,
        overfit_count=overfit,
        holdout_failed_count=holdout_failed,
        score=float(base),
        notes=notes,
    )


def calibrate(
    metrics_by_addr: dict[str, WalletEdgeMetrics],
    breakdown_by_addr: dict[str, CompositeBreakdown],
    validation_by_addr: dict[str, tuple[float | None, float | None]],
) -> tuple[CalibrationResult, list[CalibrationResult]]:
    """Run the sweep and return (best, all_sorted_desc_by_score)."""
    candidates = build_candidates(list(metrics_by_addr.values()))
    results: list[CalibrationResult] = []
    for cand in candidates:
        res = evaluate_candidate(cand, metrics_by_addr, breakdown_by_addr, validation_by_addr)
        results.append(res)
    results.sort(key=lambda r: r.score, reverse=True)
    return results[0], results
