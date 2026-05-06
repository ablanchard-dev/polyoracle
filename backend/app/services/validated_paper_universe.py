"""Merged universe of OOS-validated wallets across multiple discovery runs.

Reads two (or more) ``candidate_elite_validation_report_*.json`` payloads,
keeps only wallets that the bot is willing to copy, and emits a single ranked
CSV that the paper engine can read at startup.

Inclusion rules (all must hold per row):

* ``candidate_status`` ∈ {``ELITE``, ``STRONG``}
* ``expanded_sample`` (= total resolved markets) ≥ 30 for STRONG, ≥ 100 for ELITE
* ``win_rate_confidence`` ∈ {``MEDIUM``, ``HIGH``} for STRONG, exactly ``HIGH`` for ELITE
* ``validation_sample`` ≥ 5 (≥ 10 for ELITE) and ``validation_win_rate`` known

Hard exclusions (any of):

* ``OUTLIER_FLAGGED``    — discovery↔validation gap > 15% or val WR < 70%.
* ``BIASED_SAMPLE``      — single event-slug correlation, single-category, etc.
* ``FAILED_VALIDATION``  — discovery edge collapsed in the validation partition.
* ``CANDIDATE_ELITE``    — sample still under 30 resolved markets.
* ``DROPPED`` / ``INSUFFICIENT_DATA``.

Per-row ``allowed_*`` flags drive the live risk-mode gate at copy time:

* ``allowed_safe``        ⇔ ELITE + |gap| ≤ 0.10 + validation_wr ≥ 0.80.
* ``allowed_aggressive``  ⇔ ELITE OR STRONG (validated, no outlier).
* ``allowed_full_paper``  ⇔ ELITE OR STRONG (validated, no outlier / no failed / no biased).
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.config import get_settings

logger = logging.getLogger(__name__)


CSV_COLUMNS = [
    "rank",
    "address",
    "tier",
    "candidate_status",
    "total_sample",
    "total_win_rate",
    "validation_sample",
    "validation_win_rate",
    "validation_gap",
    "discovery_run_source",
    "last_validated_at",
    "allowed_safe",
    "allowed_aggressive",
    "allowed_full_paper",
    "warnings",
]


@dataclass
class UniverseEntry:
    address: str
    tier: str  # "ELITE" or "STRONG"
    candidate_status: str
    total_sample: int
    total_win_rate: float
    validation_sample: int
    validation_win_rate: float | None
    validation_gap: float | None
    discovery_run_source: str  # e.g. "730d", "1095d", "730d+1095d"
    last_validated_at: str
    allowed_safe: bool
    allowed_aggressive: bool
    allowed_full_paper: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "tier": self.tier,
            "candidate_status": self.candidate_status,
            "total_sample": self.total_sample,
            "total_win_rate": self.total_win_rate,
            "validation_sample": self.validation_sample,
            "validation_win_rate": self.validation_win_rate,
            "validation_gap": self.validation_gap,
            "discovery_run_source": self.discovery_run_source,
            "last_validated_at": self.last_validated_at,
            "allowed_safe": self.allowed_safe,
            "allowed_aggressive": self.allowed_aggressive,
            "allowed_full_paper": self.allowed_full_paper,
            "warnings": list(self.warnings),
        }


@dataclass
class UniverseSummary:
    generated_at: str
    sources: list[str]
    total_entries: int
    elite_count: int
    strong_count: int
    allowed_safe_count: int
    allowed_aggressive_count: int
    allowed_full_paper_count: int
    excluded_outlier_count: int
    excluded_biased_count: int
    excluded_failed_count: int
    excluded_candidate_count: int
    csv_path: str
    latest_csv_path: str
    json_path: str
    top_addresses: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "sources": list(self.sources),
            "total_entries": self.total_entries,
            "elite_count": self.elite_count,
            "strong_count": self.strong_count,
            "allowed_safe_count": self.allowed_safe_count,
            "allowed_aggressive_count": self.allowed_aggressive_count,
            "allowed_full_paper_count": self.allowed_full_paper_count,
            "excluded_outlier_count": self.excluded_outlier_count,
            "excluded_biased_count": self.excluded_biased_count,
            "excluded_failed_count": self.excluded_failed_count,
            "excluded_candidate_count": self.excluded_candidate_count,
            "csv_path": self.csv_path,
            "latest_csv_path": self.latest_csv_path,
            "json_path": self.json_path,
            "top_addresses": list(self.top_addresses),
        }


class ValidatedPaperUniverse:
    """Merge several ``candidate_elite_validation_report_*.json`` files into a
    single universe with per-row ``allowed_*`` decisions for the risk modes."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.exports_dir = self.settings.resolved_sqlite_path.parent / "exports"
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    @property
    def latest_csv_path(self) -> Path:
        return self.exports_dir / "validated_paper_universe_latest.csv"

    @property
    def latest_json_path(self) -> Path:
        return self.exports_dir / "validated_paper_universe_latest.json"

    def latest_summary(self) -> dict[str, Any] | None:
        if not self.latest_json_path.exists():
            return None
        try:
            return json.loads(self.latest_json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Cannot read latest universe summary: %s", exc)
            return None

    def merge(
        self,
        sources: Sequence[tuple[str, Path]],
        *,
        suffix: str = "",
    ) -> UniverseSummary:
        """``sources`` is a list of ``(label, json_path)`` tuples — typically
        ``[("730d", path_730), ("1095d", path_1095)]``. The label drives the
        ``discovery_run_source`` column on each emitted row."""
        per_address: dict[str, dict[str, Any]] = {}
        excluded_outlier = 0
        excluded_biased = 0
        excluded_failed = 0
        excluded_candidate = 0
        for label, path in sources:
            payload = self._read_source(path)
            if not payload:
                logger.warning("Universe merge: source %s missing or empty", path)
                continue
            for row in payload.get("rows", []):
                status = row.get("candidate_status")
                addr = (row.get("address") or "").lower()
                if not addr:
                    continue
                if status == "OUTLIER_FLAGGED":
                    excluded_outlier += 1
                    continue
                if status == "BIASED_SAMPLE":
                    excluded_biased += 1
                    continue
                if status == "FAILED_VALIDATION":
                    excluded_failed += 1
                    continue
                if status not in {"ELITE", "STRONG"}:
                    if status == "CANDIDATE_ELITE":
                        excluded_candidate += 1
                    continue
                expanded = int(row.get("expanded_sample") or 0)
                validation_sample = int(row.get("validation_sample") or 0)
                # Hard sample gates (mirror the rules in CandidateValidationService).
                if status == "ELITE" and (expanded < 100 or validation_sample < 10):
                    excluded_candidate += 1
                    continue
                if status == "STRONG" and (expanded < 30 or validation_sample < 5):
                    excluded_candidate += 1
                    continue
                # v0.5.4 belt-and-braces: legacy reports may pre-date the
                # OUTLIER_FLAGGED enum (the 730d run was produced before the
                # tighter rule landed). Re-apply the strict gap / val_wr gate
                # at merge time so a wallet stamped ELITE in an old report
                # still gets demoted if it would not be ELITE today.
                disc_wr = row.get("discovery_win_rate")
                val_wr = row.get("validation_win_rate")
                if status == "ELITE" and val_wr is not None:
                    if val_wr < 0.70:
                        excluded_outlier += 1
                        continue
                    if disc_wr is not None and abs(disc_wr - val_wr) > 0.15:
                        excluded_outlier += 1
                        continue
                self._merge_row(per_address, label, row)

        entries = self._build_entries(per_address)
        entries.sort(key=lambda e: (0 if e.tier == "ELITE" else 1, -e.total_sample, -(e.total_win_rate or 0)))

        elite_count = sum(1 for e in entries if e.tier == "ELITE")
        strong_count = sum(1 for e in entries if e.tier == "STRONG")
        allowed_safe = sum(1 for e in entries if e.allowed_safe)
        allowed_aggressive = sum(1 for e in entries if e.allowed_aggressive)
        allowed_full = sum(1 for e in entries if e.allowed_full_paper)

        suffix_part = f"_{suffix.strip('_')}" if suffix else ""
        csv_path = self.exports_dir / f"validated_paper_universe{suffix_part}.csv"
        json_path = self.exports_dir / f"validated_paper_universe{suffix_part}.json"
        self._write_csv(csv_path, entries)
        # Also write JSON snapshot + alias to "latest".
        summary = UniverseSummary(
            generated_at=datetime.now(UTC).isoformat(),
            sources=[label for label, _ in sources],
            total_entries=len(entries),
            elite_count=elite_count,
            strong_count=strong_count,
            allowed_safe_count=allowed_safe,
            allowed_aggressive_count=allowed_aggressive,
            allowed_full_paper_count=allowed_full,
            excluded_outlier_count=excluded_outlier,
            excluded_biased_count=excluded_biased,
            excluded_failed_count=excluded_failed,
            excluded_candidate_count=excluded_candidate,
            csv_path=str(csv_path),
            latest_csv_path=str(self.latest_csv_path),
            json_path=str(json_path),
            top_addresses=[e.address for e in entries[:50]],
        )
        json_path.write_text(
            json.dumps(
                {**summary.to_dict(), "rows": [e.to_dict() for e in entries]},
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        # Refresh "latest" alias copies (Windows-friendly).
        try:
            shutil.copy2(csv_path, self.latest_csv_path)
            shutil.copy2(json_path, self.latest_json_path)
        except Exception as exc:
            logger.warning("Could not refresh latest universe alias: %s", exc)
        return summary

    # ---------------- internals ----------------

    def _read_source(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Universe merge: cannot read %s (%s)", path, exc)
            return None

    def _merge_row(self, store: dict[str, dict[str, Any]], label: str, row: dict[str, Any]) -> None:
        addr = (row.get("address") or "").lower()
        existing = store.get(addr)
        if existing is None:
            store[addr] = {"row": row, "labels": {label}, "best_at": datetime.now(UTC).isoformat()}
        elif self._is_better(row, existing.get("row")):
            # Keep the better row but preserve every source label that already saw this wallet.
            existing_labels = set(existing.get("labels") or set())
            existing_labels.add(label)
            store[addr] = {"row": row, "labels": existing_labels, "best_at": datetime.now(UTC).isoformat()}
        else:
            existing["labels"].add(label)

    def _is_better(self, candidate: dict[str, Any], current: dict[str, Any] | None) -> bool:
        if current is None:
            return True
        # Prefer ELITE over STRONG, otherwise more sample.
        cand_status = candidate.get("candidate_status")
        curr_status = current.get("candidate_status")
        cand_rank = 0 if cand_status == "ELITE" else 1
        curr_rank = 0 if curr_status == "ELITE" else 1
        if cand_rank != curr_rank:
            return cand_rank < curr_rank
        return int(candidate.get("expanded_sample") or 0) > int(current.get("expanded_sample") or 0)

    def _build_entries(self, store: dict[str, dict[str, Any]]) -> list[UniverseEntry]:
        entries: list[UniverseEntry] = []
        for addr, data in store.items():
            row = data["row"]
            labels = sorted(data["labels"])
            tier = row.get("candidate_status") if row.get("candidate_status") in {"ELITE", "STRONG"} else "STRONG"
            sample = int(row.get("expanded_sample") or 0)
            wr = float(row.get("expanded_win_rate") or 0)
            val_sample = int(row.get("validation_sample") or 0)
            val_wr = row.get("validation_win_rate")
            disc_wr = row.get("discovery_win_rate")
            gap = (
                abs((disc_wr or 0) - (val_wr or 0))
                if disc_wr is not None and val_wr is not None
                else None
            )
            warnings: list[str] = list(row.get("warnings") or [])
            allowed_safe = (
                tier == "ELITE"
                and gap is not None
                and gap <= 0.10
                and val_wr is not None
                and val_wr >= 0.80
            )
            allowed_aggressive = tier in {"ELITE", "STRONG"}
            allowed_full_paper = allowed_aggressive
            entries.append(
                UniverseEntry(
                    address=addr,
                    tier=tier,
                    candidate_status=str(row.get("candidate_status") or "UNKNOWN"),
                    total_sample=sample,
                    total_win_rate=wr,
                    validation_sample=val_sample,
                    validation_win_rate=val_wr,
                    validation_gap=gap,
                    discovery_run_source="+".join(labels),
                    last_validated_at=data.get("best_at", datetime.now(UTC).isoformat()),
                    allowed_safe=bool(allowed_safe),
                    allowed_aggressive=bool(allowed_aggressive),
                    allowed_full_paper=bool(allowed_full_paper),
                    warnings=warnings,
                )
            )
        return entries

    def _write_csv(self, path: Path, entries: Iterable[UniverseEntry]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for rank, entry in enumerate(entries, start=1):
                payload = entry.to_dict()
                payload["rank"] = rank
                payload["warnings"] = ";".join(payload["warnings"])
                writer.writerow(payload)
