"""v0.5.6 Phase A — ValidatedPaperUniverseV056.

Reconstructs the universe from :class:`WalletEdgeMetrics` +
:class:`CompositeBreakdown` + :class:`TierDecision`, applying the
"0 faux ELITE" guarantee:

* a wallet is **only ELITE** if it passes every hard gate in
  :data:`ELITE_RULES` (real-EV bootstrap p20, persistence,
  holdout, sample, validation, no blocking status).
* allowed_safe is granted only to ELITE rows above the p85 EV-lower-bound
  cut (or the empirically-derived threshold) with persistence ≥ 0.75 and
  activity_recency ≥ 0.60.

Inputs required per wallet:

* WalletEdgeMetrics       (real-EV, persistence, holdout, etc.)
* CompositeBreakdown      (composite 0..100)
* TierDecision            (ELITE / STRONG / CANDIDATE_EDGE / WATCH / IGNORE / SUSPICIOUS)
* Optional validation row from CandidateValidationService (val_wr, val_gap)
* Optional prior_tier from v0.5.5 universe (so demotions can be flagged)
* Optional lost_gem_action (REINTEGRATE / INVESTIGATE) from v0.5.5.1 audit

Outputs:

* ``data/exports/validated_paper_universe_v0_5_6.csv``     — the new universe
* ``data/exports/validated_paper_universe_v0_5_6.json``    — full snapshot
* ``data/exports/false_elite_removed.csv``                 — prior ELITE
                                                            now demoted/dropped
* ``data/exports/lost_gems_reintegrated.csv``              — lost gems brought
                                                            back through gates
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.composite_wallet_score import CompositeBreakdown, TierDecision
from app.services.edge_quality_engine import WalletEdgeMetrics

logger = logging.getLogger(__name__)


CSV_COLUMNS = [
    "rank",
    "address",
    "tier_v0_5_6",
    "prior_tier",
    "composite_score",
    "sample_size",
    "win_rate",
    "expected_value",
    "ev_lower_bound",
    "ev_confidence",
    "average_entry_price",
    "price_entry_sanity",
    "persistence_score",
    "holdout_pass",
    "holdout_verdict",
    "validation_win_rate",
    "validation_gap",
    "category_count",
    "best_category",
    "best_category_ev",
    "regime_fraction_positive",
    "activity_recency_weeks",
    "estimated_trades_per_week",
    "expected_weekly_R",
    "lost_gem_action",
    "discovery_run_source",
    "allowed_safe",
    "allowed_aggressive",
    "allowed_full_paper",
    "exclusion_status",
    "rules_failed",
    "warnings",
]


TIER_LABELS = {
    "ELITE": "ELITE_EV_VALIDATED",
    "STRONG": "STRONG_EV_VALIDATED",
    "CANDIDATE_EDGE": "CANDIDATE_EDGE",
    "WATCH": "WATCH_HIGH_EV",
    "IGNORE": "IGNORE",
    "SUSPICIOUS": "SUSPICIOUS",
}


@dataclass
class WalletInputs:
    address: str
    metrics: WalletEdgeMetrics
    breakdown: CompositeBreakdown
    decision: TierDecision
    prior_tier: str | None = None
    validation_win_rate: float | None = None
    validation_gap: float | None = None
    lost_gem_action: str | None = None  # REINTEGRATE | INVESTIGATE | None
    discovery_run_source: str = ""


@dataclass
class UniverseEntryV2:
    address: str
    tier_v0_5_6: str
    prior_tier: str | None
    composite_score: float
    sample_size: int
    win_rate: float | None
    expected_value: float | None
    ev_lower_bound: float | None
    ev_confidence: str
    average_entry_price: float | None
    price_entry_sanity: str
    persistence_score: float
    holdout_pass: bool
    holdout_verdict: str | None
    validation_win_rate: float | None
    validation_gap: float | None
    category_count: int
    best_category: str | None
    best_category_ev: float | None
    regime_fraction_positive: float | None
    activity_recency_weeks: float | None
    estimated_trades_per_week: float | None
    expected_weekly_R: float | None
    lost_gem_action: str | None
    discovery_run_source: str
    allowed_safe: bool
    allowed_aggressive: bool
    allowed_full_paper: bool
    exclusion_status: list[str] = field(default_factory=list)
    rules_failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "tier_v0_5_6": self.tier_v0_5_6,
            "prior_tier": self.prior_tier,
            "composite_score": self.composite_score,
            "sample_size": self.sample_size,
            "win_rate": self.win_rate,
            "expected_value": self.expected_value,
            "ev_lower_bound": self.ev_lower_bound,
            "ev_confidence": self.ev_confidence,
            "average_entry_price": self.average_entry_price,
            "price_entry_sanity": self.price_entry_sanity,
            "persistence_score": self.persistence_score,
            "holdout_pass": self.holdout_pass,
            "holdout_verdict": self.holdout_verdict,
            "validation_win_rate": self.validation_win_rate,
            "validation_gap": self.validation_gap,
            "category_count": self.category_count,
            "best_category": self.best_category,
            "best_category_ev": self.best_category_ev,
            "regime_fraction_positive": self.regime_fraction_positive,
            "activity_recency_weeks": self.activity_recency_weeks,
            "estimated_trades_per_week": self.estimated_trades_per_week,
            "expected_weekly_R": self.expected_weekly_R,
            "lost_gem_action": self.lost_gem_action,
            "discovery_run_source": self.discovery_run_source,
            "allowed_safe": self.allowed_safe,
            "allowed_aggressive": self.allowed_aggressive,
            "allowed_full_paper": self.allowed_full_paper,
            "exclusion_status": list(self.exclusion_status),
            "rules_failed": list(self.rules_failed),
            "warnings": list(self.warnings),
        }


@dataclass
class UniverseV2Summary:
    generated_at: str
    n_total: int
    n_elite: int
    n_strong: int
    n_candidate_edge: int
    n_watch: int
    n_ignore: int
    n_suspicious: int
    n_allowed_safe: int
    n_allowed_aggressive: int
    n_allowed_full_paper: int
    n_false_elite_removed: int
    n_lost_gems_reintegrated: int
    elite_threshold_composite: float
    strong_threshold_composite: float
    elite_threshold_ev_lower_bound: float
    strong_threshold_ev_lower_bound: float
    csv_path: str
    json_path: str
    false_elite_path: str
    lost_gems_path: str


def _safe_top_category(category_edge_map: dict[str, dict[str, Any]]) -> tuple[str | None, float | None]:
    if not category_edge_map:
        return None, None
    sorted_cats = sorted(
        category_edge_map.items(),
        key=lambda kv: (kv[1].get("sample") or 0),
        reverse=True,
    )
    cat, info = sorted_cats[0]
    return cat, info.get("ev")


class ValidatedPaperUniverseV056:
    def __init__(
        self,
        exports_dir: Path,
        *,
        elite_safe_min_ev_lower_bound: float = 0.05,
        elite_safe_min_persistence: float = 0.75,
        elite_safe_min_recency_score: float = 0.60,
    ) -> None:
        self.exports_dir = Path(exports_dir)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.elite_safe_min_ev_lower_bound = elite_safe_min_ev_lower_bound
        self.elite_safe_min_persistence = elite_safe_min_persistence
        self.elite_safe_min_recency_score = elite_safe_min_recency_score

    def build(
        self,
        inputs: list[WalletInputs],
        *,
        elite_threshold_composite: float,
        strong_threshold_composite: float,
        elite_threshold_ev_lower_bound: float,
        strong_threshold_ev_lower_bound: float,
        suffix: str = "v0_5_6",
    ) -> UniverseV2Summary:
        entries: list[UniverseEntryV2] = []
        false_elites: list[UniverseEntryV2] = []
        reintegrated: list[UniverseEntryV2] = []

        for inp in inputs:
            entry = self._build_entry(inp)
            entries.append(entry)
            # False ELITE = was ELITE before, no longer ELITE.
            if inp.prior_tier == "ELITE" and entry.tier_v0_5_6 != "ELITE_EV_VALIDATED":
                false_elites.append(entry)
            # Reintegrated lost gems = was not in prior universe, now ELITE/STRONG/CANDIDATE.
            if inp.prior_tier is None and entry.tier_v0_5_6 in {
                "ELITE_EV_VALIDATED",
                "STRONG_EV_VALIDATED",
                "CANDIDATE_EDGE",
            }:
                reintegrated.append(entry)

        entries.sort(
            key=lambda e: (
                {"ELITE_EV_VALIDATED": 0, "STRONG_EV_VALIDATED": 1, "CANDIDATE_EDGE": 2,
                 "WATCH_HIGH_EV": 3, "SUSPICIOUS": 4, "IGNORE": 5}.get(e.tier_v0_5_6, 6),
                -(e.composite_score or 0),
                -(e.ev_lower_bound or -1),
            )
        )

        n_elite = sum(1 for e in entries if e.tier_v0_5_6 == "ELITE_EV_VALIDATED")
        n_strong = sum(1 for e in entries if e.tier_v0_5_6 == "STRONG_EV_VALIDATED")
        n_candidate = sum(1 for e in entries if e.tier_v0_5_6 == "CANDIDATE_EDGE")
        n_watch = sum(1 for e in entries if e.tier_v0_5_6 == "WATCH_HIGH_EV")
        n_ignore = sum(1 for e in entries if e.tier_v0_5_6 == "IGNORE")
        n_suspicious = sum(1 for e in entries if e.tier_v0_5_6 == "SUSPICIOUS")

        csv_path = self.exports_dir / f"validated_paper_universe_{suffix}.csv"
        json_path = self.exports_dir / f"validated_paper_universe_{suffix}.json"
        false_path = self.exports_dir / "false_elite_removed.csv"
        gems_path = self.exports_dir / "lost_gems_reintegrated.csv"

        self._write_csv(csv_path, entries)
        self._write_csv(false_path, false_elites)
        self._write_csv(gems_path, reintegrated)

        json_payload: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "thresholds": {
                "elite_min_composite": elite_threshold_composite,
                "strong_min_composite": strong_threshold_composite,
                "elite_min_ev_lower_bound": elite_threshold_ev_lower_bound,
                "strong_min_ev_lower_bound": strong_threshold_ev_lower_bound,
                "elite_safe_min_ev_lower_bound": self.elite_safe_min_ev_lower_bound,
                "elite_safe_min_persistence": self.elite_safe_min_persistence,
                "elite_safe_min_recency_score": self.elite_safe_min_recency_score,
            },
            "summary": {
                "n_total": len(entries),
                "n_elite": n_elite,
                "n_strong": n_strong,
                "n_candidate_edge": n_candidate,
                "n_watch": n_watch,
                "n_ignore": n_ignore,
                "n_suspicious": n_suspicious,
                "n_allowed_safe": sum(1 for e in entries if e.allowed_safe),
                "n_allowed_aggressive": sum(1 for e in entries if e.allowed_aggressive),
                "n_allowed_full_paper": sum(1 for e in entries if e.allowed_full_paper),
                "n_false_elite_removed": len(false_elites),
                "n_lost_gems_reintegrated": len(reintegrated),
            },
            "rows": [e.to_dict() for e in entries],
        }
        json_path.write_text(json.dumps(json_payload, indent=2, default=str), encoding="utf-8")

        return UniverseV2Summary(
            generated_at=json_payload["generated_at"],
            n_total=len(entries),
            n_elite=n_elite,
            n_strong=n_strong,
            n_candidate_edge=n_candidate,
            n_watch=n_watch,
            n_ignore=n_ignore,
            n_suspicious=n_suspicious,
            n_allowed_safe=json_payload["summary"]["n_allowed_safe"],
            n_allowed_aggressive=json_payload["summary"]["n_allowed_aggressive"],
            n_allowed_full_paper=json_payload["summary"]["n_allowed_full_paper"],
            n_false_elite_removed=len(false_elites),
            n_lost_gems_reintegrated=len(reintegrated),
            elite_threshold_composite=elite_threshold_composite,
            strong_threshold_composite=strong_threshold_composite,
            elite_threshold_ev_lower_bound=elite_threshold_ev_lower_bound,
            strong_threshold_ev_lower_bound=strong_threshold_ev_lower_bound,
            csv_path=str(csv_path),
            json_path=str(json_path),
            false_elite_path=str(false_path),
            lost_gems_path=str(gems_path),
        )

    def _build_entry(self, inp: WalletInputs) -> UniverseEntryV2:
        m = inp.metrics
        bk = inp.breakdown
        dec = inp.decision
        cat, cat_ev = _safe_top_category(m.category_edge_map)

        tier_label = TIER_LABELS.get(dec.tier, "IGNORE")
        # Hard guarantee: a wallet with any blocking status from
        # BLOCK_ANY_TIER cannot be ELITE_EV_VALIDATED nor STRONG_EV_VALIDATED.
        # apply_tier_rules already enforces this — defensive double-check here:
        if dec.blocking_statuses and tier_label in {"ELITE_EV_VALIDATED", "STRONG_EV_VALIDATED"}:
            tier_label = "IGNORE"

        # allowed_safe gate: ELITE only, very conservative.
        allowed_safe = (
            tier_label == "ELITE_EV_VALIDATED"
            and (m.ev_lower_bound or -1) >= self.elite_safe_min_ev_lower_bound
            and m.persistence_score >= self.elite_safe_min_persistence
            and m.activity_recency_score >= self.elite_safe_min_recency_score
            and m.holdout_pass
            and not dec.blocking_statuses
        )
        allowed_aggressive = tier_label in {"ELITE_EV_VALIDATED", "STRONG_EV_VALIDATED"}
        allowed_full_paper = tier_label in {
            "ELITE_EV_VALIDATED",
            "STRONG_EV_VALIDATED",
            "CANDIDATE_EDGE",
            "WATCH_HIGH_EV",
        } and (m.expected_value or 0) > 0

        warnings: list[str] = []
        if inp.lost_gem_action == "REINTEGRATE":
            warnings.append("REINTEGRATED_FROM_v0_5_5_1_AUDIT")
        if inp.lost_gem_action == "INVESTIGATE":
            warnings.append("INVESTIGATE_LOW_VAL_SAMPLE")

        return UniverseEntryV2(
            address=inp.address,
            tier_v0_5_6=tier_label,
            prior_tier=inp.prior_tier,
            composite_score=bk.composite,
            sample_size=m.sample_size,
            win_rate=m.win_rate,
            expected_value=m.expected_value,
            ev_lower_bound=m.ev_lower_bound,
            ev_confidence=m.ev_confidence,
            average_entry_price=m.average_entry_price,
            price_entry_sanity=m.price_entry_sanity,
            persistence_score=m.persistence_score,
            holdout_pass=m.holdout_pass,
            holdout_verdict=(m.holdout_details or {}).get("verdict"),
            validation_win_rate=inp.validation_win_rate,
            validation_gap=inp.validation_gap,
            category_count=len(m.category_edge_map),
            best_category=cat,
            best_category_ev=cat_ev,
            regime_fraction_positive=(m.regime_robustness or {}).get("fraction_positive"),
            activity_recency_weeks=m.activity_recency_weeks,
            estimated_trades_per_week=m.estimated_trades_per_week,
            expected_weekly_R=m.expected_weekly_R,
            lost_gem_action=inp.lost_gem_action,
            discovery_run_source=inp.discovery_run_source,
            allowed_safe=bool(allowed_safe),
            allowed_aggressive=bool(allowed_aggressive),
            allowed_full_paper=bool(allowed_full_paper),
            exclusion_status=list(m.exclusion_status),
            rules_failed=list(dec.rules_failed),
            warnings=warnings,
        )

    def _write_csv(self, path: Path, entries: list[UniverseEntryV2]) -> None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for rank, entry in enumerate(entries, start=1):
                row = entry.to_dict()
                row["rank"] = rank
                row["exclusion_status"] = ";".join(row["exclusion_status"])
                row["rules_failed"] = ";".join(row["rules_failed"])
                row["warnings"] = ";".join(row["warnings"])
                writer.writerow(row)
