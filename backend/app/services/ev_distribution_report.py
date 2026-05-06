"""v0.5.6 Phase A — EV distribution report.

Reads :class:`WalletEdgeMetrics` for the cohort and emits two files:

* ``data/exports/ev_distribution_report.json`` — full payload with
  percentile cuts, per-tier slices, sample-bucket distribution, and
  proposed empirical thresholds.
* ``data/exports/ev_distribution_report.csv``  — flat per-wallet rows
  (address, tier from prior run, EV, EV_lb, sample, composite) so the
  user can sort / filter in a spreadsheet.

The report is descriptive, not prescriptive: the proposed thresholds
are returned with the data and consumed by the driver, not hard-coded
into the universe builder.
"""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.services.composite_wallet_score import CompositeBreakdown
from app.services.edge_quality_engine import WalletEdgeMetrics


PERCENTILES = [50, 60, 75, 85, 90, 95]


@dataclass
class DistributionRow:
    address: str
    prior_tier: str | None
    sample: int
    win_rate: float | None
    expected_value: float | None
    ev_lower_bound: float | None
    composite: float | None
    persistence_score: float
    holdout_pass: bool
    exclusion_status: list[str]

    def to_csv_row(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "prior_tier": self.prior_tier or "",
            "sample": self.sample,
            "win_rate": self.win_rate,
            "expected_value": self.expected_value,
            "ev_lower_bound": self.ev_lower_bound,
            "composite": self.composite,
            "persistence_score": self.persistence_score,
            "holdout_pass": self.holdout_pass,
            "exclusion_status": ";".join(self.exclusion_status),
        }


@dataclass
class DistributionReport:
    n_wallets: int
    n_with_ev: int
    n_with_ev_lower_bound: int
    ev_percentiles: dict[str, float]
    ev_lower_bound_percentiles: dict[str, float]
    composite_percentiles: dict[str, float]
    by_sample_bucket: dict[str, dict[str, float | int]]
    by_prior_tier: dict[str, dict[str, float | int]]
    proposed_thresholds: dict[str, float]
    rows: list[DistributionRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_wallets": self.n_wallets,
            "n_with_ev": self.n_with_ev,
            "n_with_ev_lower_bound": self.n_with_ev_lower_bound,
            "ev_percentiles": self.ev_percentiles,
            "ev_lower_bound_percentiles": self.ev_lower_bound_percentiles,
            "composite_percentiles": self.composite_percentiles,
            "by_sample_bucket": self.by_sample_bucket,
            "by_prior_tier": self.by_prior_tier,
            "proposed_thresholds": self.proposed_thresholds,
        }


def _percentiles(values: list[float], pcts: Iterable[int]) -> dict[str, float]:
    out: dict[str, float] = {}
    if not values:
        return {f"p{p}": float("nan") for p in pcts}
    sv = sorted(values)
    n = len(sv)
    for p in pcts:
        # Nearest-rank style — robust for small samples.
        idx = max(0, min(n - 1, int(round(p / 100.0 * (n - 1)))))
        out[f"p{p}"] = round(sv[idx], 4)
    return out


def _sample_bucket(sample: int) -> str:
    if sample < 30:
        return "lt30"
    if sample < 100:
        return "30_99"
    if sample < 300:
        return "100_299"
    if sample < 1000:
        return "300_999"
    return "1000_plus"


def _bucket_summary(rows: list[DistributionRow]) -> dict[str, float | int]:
    evs = [r.expected_value for r in rows if r.expected_value is not None]
    ev_lbs = [r.ev_lower_bound for r in rows if r.ev_lower_bound is not None]
    composites = [r.composite for r in rows if r.composite is not None]
    return {
        "n": len(rows),
        "n_with_ev": len(evs),
        "ev_median": round(statistics.median(evs), 4) if evs else None,
        "ev_lb_median": round(statistics.median(ev_lbs), 4) if ev_lbs else None,
        "composite_median": round(statistics.median(composites), 2) if composites else None,
        "frac_ev_positive": round(sum(1 for e in evs if e > 0) / len(evs), 4) if evs else None,
        "frac_ev_lb_positive": round(sum(1 for e in ev_lbs if e > 0) / len(ev_lbs), 4) if ev_lbs else None,
    }


def _propose_thresholds(rows: list[DistributionRow]) -> dict[str, float]:
    """Empirical baseline thresholds — driven by the rejection target.

    We aim for an ELITE tier that contains roughly the top 5-15 % of
    *positive-EV-lower-bound* wallets; STRONG aims for the next ~15-25 %.
    The numbers are descriptive seeds — the driver still applies hard
    gates on top.
    """
    valid = [r for r in rows if r.ev_lower_bound is not None and r.expected_value is not None]
    valid.sort(key=lambda r: r.ev_lower_bound or 0, reverse=True)
    if not valid:
        return {
            "elite_min_ev_lower_bound": 0.05,
            "strong_min_ev_lower_bound": 0.0,
            "elite_min_composite": 75.0,
            "strong_min_composite": 60.0,
        }
    n = len(valid)
    elite_idx = max(0, min(n - 1, int(0.85 * n)))  # p85 of EV_lb
    strong_idx = max(0, min(n - 1, int(0.50 * n)))  # p50 of EV_lb
    elite_ev_lb_threshold = max(0.01, valid[n - 1 - elite_idx].ev_lower_bound or 0)
    strong_ev_lb_threshold = max(0.0, valid[n - 1 - strong_idx].ev_lower_bound or 0)

    composites = sorted([r.composite for r in valid if r.composite is not None], reverse=True)
    if composites:
        elite_comp_threshold = composites[max(0, min(len(composites) - 1, int(0.15 * len(composites))))]
        strong_comp_threshold = composites[max(0, min(len(composites) - 1, int(0.50 * len(composites))))]
    else:
        elite_comp_threshold = 75.0
        strong_comp_threshold = 60.0
    return {
        "elite_min_ev_lower_bound": round(max(0.01, elite_ev_lb_threshold), 4),
        "strong_min_ev_lower_bound": round(max(0.0, strong_ev_lb_threshold), 4),
        "elite_min_composite": round(max(75.0, float(elite_comp_threshold)), 2),
        "strong_min_composite": round(max(55.0, float(strong_comp_threshold)), 2),
    }


def build_report(
    metrics_by_addr: dict[str, WalletEdgeMetrics],
    breakdown_by_addr: dict[str, CompositeBreakdown],
    prior_tier_by_addr: dict[str, str | None],
) -> DistributionReport:
    rows: list[DistributionRow] = []
    for addr, m in metrics_by_addr.items():
        bk = breakdown_by_addr.get(addr)
        rows.append(
            DistributionRow(
                address=addr,
                prior_tier=prior_tier_by_addr.get(addr),
                sample=m.sample_size,
                win_rate=m.win_rate,
                expected_value=m.expected_value,
                ev_lower_bound=m.ev_lower_bound,
                composite=bk.composite if bk else None,
                persistence_score=m.persistence_score,
                holdout_pass=m.holdout_pass,
                exclusion_status=list(m.exclusion_status),
            )
        )
    evs = [r.expected_value for r in rows if r.expected_value is not None]
    ev_lbs = [r.ev_lower_bound for r in rows if r.ev_lower_bound is not None]
    composites = [r.composite for r in rows if r.composite is not None]

    by_bucket: dict[str, dict[str, float | int]] = {}
    for bucket in ["lt30", "30_99", "100_299", "300_999", "1000_plus"]:
        slab = [r for r in rows if _sample_bucket(r.sample) == bucket]
        by_bucket[bucket] = _bucket_summary(slab)

    by_prior_tier: dict[str, dict[str, float | int]] = {}
    for tier in ["ELITE", "STRONG", None]:
        slab = [r for r in rows if r.prior_tier == tier]
        if not slab:
            continue
        key = tier or "OTHER_OR_NONE"
        by_prior_tier[key] = _bucket_summary(slab)

    return DistributionReport(
        n_wallets=len(rows),
        n_with_ev=len(evs),
        n_with_ev_lower_bound=len(ev_lbs),
        ev_percentiles=_percentiles(evs, PERCENTILES),
        ev_lower_bound_percentiles=_percentiles(ev_lbs, PERCENTILES),
        composite_percentiles=_percentiles(composites, PERCENTILES),
        by_sample_bucket=by_bucket,
        by_prior_tier=by_prior_tier,
        proposed_thresholds=_propose_thresholds(rows),
        rows=rows,
    )


def write_report(
    report: DistributionReport,
    json_path: Path,
    csv_path: Path,
) -> None:
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )
    columns = [
        "address",
        "prior_tier",
        "sample",
        "win_rate",
        "expected_value",
        "ev_lower_bound",
        "composite",
        "persistence_score",
        "holdout_pass",
        "exclusion_status",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in sorted(report.rows, key=lambda r: (-(r.composite or 0), -(r.ev_lower_bound or -1))):
            writer.writerow(r.to_csv_row())
