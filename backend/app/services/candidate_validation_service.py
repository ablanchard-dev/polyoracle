"""Validate the wallets that v0.5 market-first discovery flagged as promising.

The v0.5 first run found 23 wallets with 85-100% resolved-market win rate but
LOW confidence (sample 12-23). This service answers two questions about that
short-list:

1. **Do they survive a larger sample?** Re-run the discovery with more
   markets, recompute win rate on the expanded set, and compare with the
   original numbers stored in ``data/exports/market_first_discovery_report.json``.

2. **Do they survive an out-of-sample split?** Sort the full set of usable
   resolved markets by ``end_date`` ascending, take the oldest 70% as the
   "discovery" partition, and the newest 30% as the "validation" partition.
   A real edge holds in both partitions; an artefact collapses in
   validation.

Anti-bias warnings are layered on top — see
``MarketFirstDiscoveryService.detect_*`` helpers — so we can tell when an
"impressive" candidate is actually riding on a single event slug, a single
category, or the dominance of 5-minute crypto coinflip markets in the feed.

Statuses:

    ELITE              — sample ≥ 100 + win_rate stays ≥ 0.65 in BOTH
                         partitions + no strong bias warnings.
    STRONG             — sample ≥ 30 + MEDIUM/HIGH confidence + win_rate
                         holds within 0.20 across partitions.
    CANDIDATE_ELITE    — high win rate but sample still < 30 OR validation
                         sample < 5; promising, not validated.
    BIASED_SAMPLE      — high win rate but tripped one of the bias filters
                         (single-event slug, single-category, 5-min crypto
                         dominance). Cannot be promoted.
    FAILED_VALIDATION  — strong on the discovery partition but win rate
                         collapses below 0.50 on the validation partition.
                         Bot is honest about flaky signal.
    DROPPED            — sample big enough to judge, win rate now under
                         0.55 → no edge.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Literal

from sqlmodel import Session

from app.config import get_settings
from app.models.wallet import MarketFirstWalletRecord
from app.services.market_first_discovery import (
    MarketFirstDiscoveryService,
    WalletAggregate,
)
from app.services.market_resolution_scanner import ResolvedMarket
from app.services.win_rate_engine import compute_win_rate_confidence

logger = logging.getLogger(__name__)


CandidateStatus = Literal[
    "ELITE",
    "STRONG",
    "CANDIDATE_ELITE",
    "OUTLIER_FLAGGED",
    "BIASED_SAMPLE",
    "FAILED_VALIDATION",
    "DROPPED",
]


CSV_COLUMNS = [
    "address",
    "previous_sample",
    "previous_win_rate",
    "expanded_sample",
    "expanded_win_rate",
    "discovery_sample",
    "discovery_win_rate",
    "validation_sample",
    "validation_win_rate",
    "category_breakdown",
    "best_category",
    "category_concentration_share",
    "market_correlation_warning",
    "market_correlation_share",
    "data_quality_warning",
    "candidate_status",
    "final_recommendation",
    "warnings",
    "data_source",
]


@dataclass
class CandidateRow:
    address: str
    previous_sample: int | None
    previous_win_rate: float | None
    expanded_sample: int
    expanded_win_rate: float | None
    discovery_sample: int
    discovery_win_rate: float | None
    validation_sample: int
    validation_win_rate: float | None
    category_breakdown: dict[str, dict[str, Any]]
    best_category: str | None
    category_concentration_share: float
    market_correlation_warning: bool
    market_correlation_share: float
    data_quality_warning: bool
    candidate_status: CandidateStatus
    final_recommendation: str
    warnings: list[str]
    data_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "previous_sample": self.previous_sample,
            "previous_win_rate": self.previous_win_rate,
            "expanded_sample": self.expanded_sample,
            "expanded_win_rate": self.expanded_win_rate,
            "discovery_sample": self.discovery_sample,
            "discovery_win_rate": self.discovery_win_rate,
            "validation_sample": self.validation_sample,
            "validation_win_rate": self.validation_win_rate,
            "category_breakdown": self.category_breakdown,
            "best_category": self.best_category,
            "category_concentration_share": self.category_concentration_share,
            "market_correlation_warning": self.market_correlation_warning,
            "market_correlation_share": self.market_correlation_share,
            "data_quality_warning": self.data_quality_warning,
            "candidate_status": self.candidate_status,
            "final_recommendation": self.final_recommendation,
            "warnings": list(self.warnings),
            "data_source": self.data_source,
        }


@dataclass
class ValidationReport:
    started_at: str
    finished_at: str
    duration_ms: float
    days_back: int
    max_markets: int
    split_ratio: float
    markets_scanned: int
    markets_usable: int
    discovery_markets: int
    validation_markets: int
    wallets_in_full: int
    previous_candidates: int
    expanded_kept: int
    elite: int
    strong: int
    candidate_elite: int
    outlier_flagged: int
    biased_sample: int
    failed_validation: int
    dropped: int
    survivor_bias_warning: bool
    survivor_bias_loser_market_share: float
    short_window_crypto_dominance: bool
    short_window_crypto_share: float
    api_errors: int
    json_path: str
    csv_path: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    top_validated: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "days_back": self.days_back,
            "max_markets": self.max_markets,
            "split_ratio": self.split_ratio,
            "markets_scanned": self.markets_scanned,
            "markets_usable": self.markets_usable,
            "discovery_markets": self.discovery_markets,
            "validation_markets": self.validation_markets,
            "wallets_in_full": self.wallets_in_full,
            "previous_candidates": self.previous_candidates,
            "expanded_kept": self.expanded_kept,
            "elite": self.elite,
            "strong": self.strong,
            "candidate_elite": self.candidate_elite,
            "outlier_flagged": self.outlier_flagged,
            "biased_sample": self.biased_sample,
            "failed_validation": self.failed_validation,
            "dropped": self.dropped,
            "survivor_bias_warning": self.survivor_bias_warning,
            "survivor_bias_loser_market_share": self.survivor_bias_loser_market_share,
            "short_window_crypto_dominance": self.short_window_crypto_dominance,
            "short_window_crypto_share": self.short_window_crypto_share,
            "api_errors": self.api_errors,
            "json_path": self.json_path,
            "csv_path": self.csv_path,
            "rows": list(self.rows),
            "top_validated": list(self.top_validated),
        }


class CandidateValidationService:
    def __init__(self, session: Session, *, export_suffix: str = "", market_first_suffix: str = "") -> None:
        self.session = session
        self.settings = get_settings()
        self.market_first = MarketFirstDiscoveryService(session, export_suffix=market_first_suffix)
        self.exports_dir = self.settings.resolved_sqlite_path.parent / "exports"
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self._export_suffix = (f"_{export_suffix.strip('_')}" if export_suffix else "")

    @property
    def json_path(self) -> Path:
        return self.exports_dir / f"candidate_elite_validation_report{self._export_suffix}.json"

    @property
    def csv_path(self) -> Path:
        return self.exports_dir / f"candidate_elite_wallets{self._export_suffix}.csv"

    @property
    def previous_report_snapshot(self) -> Path:
        return self.exports_dir / f"market_first_discovery_report_previous{self._export_suffix}.json"

    def latest_report(self) -> dict[str, Any] | None:
        if not self.json_path.exists():
            return None
        try:
            return json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read latest validation report: %s", exc)
            return None

    def export_paths(self) -> dict[str, Any]:
        return {
            "json_path": str(self.json_path),
            "json_exists": self.json_path.exists(),
            "csv_path": str(self.csv_path),
            "csv_exists": self.csv_path.exists(),
            "previous_snapshot_path": str(self.previous_report_snapshot),
            "previous_snapshot_exists": self.previous_report_snapshot.exists(),
        }

    # ---------------- run ----------------

    def run_validation(
        self,
        *,
        days_back: int = 365,
        max_markets: int = 300,
        trades_per_market: int | None = None,
        split_ratio: float = 0.7,
    ) -> ValidationReport:
        started = datetime.now(UTC)

        previous_payload = self._load_previous_report()
        previous_candidates = self._extract_previous_candidates(previous_payload)

        # Snapshot the previous v0.5 report so the validation run can overwrite
        # the live one without losing the comparison baseline.
        if self.market_first.json_path.exists() and not self.previous_report_snapshot.exists():
            shutil.copy2(self.market_first.json_path, self.previous_report_snapshot)

        split = self.market_first.run_with_temporal_split(
            days_back=days_back,
            max_markets=max_markets,
            trades_per_market=trades_per_market,
            split_ratio=split_ratio,
        )
        usable_markets: list[ResolvedMarket] = split["usable_markets"]
        discovery_markets: list[ResolvedMarket] = split["discovery_markets"]
        validation_markets: list[ResolvedMarket] = split["validation_markets"]
        full_agg: dict[str, WalletAggregate] = split["full_agg"]
        discovery_agg: dict[str, WalletAggregate] = split["discovery_agg"]
        validation_agg: dict[str, WalletAggregate] = split["validation_agg"]
        api_errors = split["api_errors"]

        markets_by_id = {m.market_id: m for m in usable_markets}

        survivor_bias_flag, loser_share = self.market_first.detect_loser_presence(full_agg, usable_markets)
        crypto_flag, crypto_share = self.market_first.detect_short_window_crypto_dominance(usable_markets)

        # Build candidate rows. We score every wallet that ended up net-long
        # the winning outcome on at least one resolved market, but flag prior
        # candidates separately so we can answer "did the v0.5 short-list
        # hold?" cleanly.
        rows: list[CandidateRow] = []
        for address, agg in full_agg.items():
            row = self._score_candidate(
                address,
                agg=agg,
                discovery_agg=discovery_agg.get(address),
                validation_agg=validation_agg.get(address),
                previous_payload=previous_candidates.get(address),
                markets_by_id=markets_by_id,
                short_window_crypto_dominance=crypto_flag,
                survivor_bias=survivor_bias_flag,
            )
            rows.append(row)
        rows.sort(key=lambda r: (-(r.expanded_sample or 0), -(r.expanded_win_rate or 0)))

        finished = datetime.now(UTC)
        duration_ms = round((finished - started).total_seconds() * 1000, 2)

        prev_addrs = set(previous_candidates.keys())
        expanded_kept = sum(1 for r in rows if r.address in prev_addrs)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.candidate_status] = counts.get(r.candidate_status, 0) + 1

        report = ValidationReport(
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_ms=duration_ms,
            days_back=days_back,
            max_markets=max_markets,
            split_ratio=split_ratio,
            markets_scanned=len(split["all_markets"]),
            markets_usable=len(usable_markets),
            discovery_markets=len(discovery_markets),
            validation_markets=len(validation_markets),
            wallets_in_full=len(full_agg),
            previous_candidates=len(prev_addrs),
            expanded_kept=expanded_kept,
            elite=counts.get("ELITE", 0),
            strong=counts.get("STRONG", 0),
            candidate_elite=counts.get("CANDIDATE_ELITE", 0),
            outlier_flagged=counts.get("OUTLIER_FLAGGED", 0),
            biased_sample=counts.get("BIASED_SAMPLE", 0),
            failed_validation=counts.get("FAILED_VALIDATION", 0),
            dropped=counts.get("DROPPED", 0),
            survivor_bias_warning=survivor_bias_flag,
            survivor_bias_loser_market_share=loser_share,
            short_window_crypto_dominance=crypto_flag,
            short_window_crypto_share=crypto_share,
            api_errors=api_errors,
            json_path=str(self.json_path),
            csv_path=str(self.csv_path),
            rows=[r.to_dict() for r in rows],
            top_validated=[
                r.to_dict()
                for r in rows
                if r.candidate_status in {"ELITE", "STRONG"}
            ][:50],
        )

        self._write_csv(rows)
        self._persist_statuses(rows)
        self.json_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        logger.info(
            "validation finished: usable=%d full_wallets=%d elite=%d strong=%d candidate=%d outlier=%d biased=%d failed=%d dropped=%d",
            len(usable_markets),
            len(full_agg),
            counts.get("ELITE", 0),
            counts.get("STRONG", 0),
            counts.get("CANDIDATE_ELITE", 0),
            counts.get("OUTLIER_FLAGGED", 0),
            counts.get("BIASED_SAMPLE", 0),
            counts.get("FAILED_VALIDATION", 0),
            counts.get("DROPPED", 0),
        )
        return report

    # ---------------- internals ----------------

    def _load_previous_report(self) -> dict[str, Any] | None:
        for candidate in (self.previous_report_snapshot, self.market_first.json_path):
            if candidate.exists():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning("Failed to read previous report %s: %s", candidate, exc)
        return None

    def _extract_previous_candidates(self, payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not payload:
            return {}
        wallets = payload.get("top_wallets", []) or []
        result: dict[str, dict[str, Any]] = {}
        for entry in wallets:
            address = entry.get("address")
            if not address:
                continue
            sample = entry.get("resolved_markets_traded", 0) or 0
            win_rate = entry.get("resolved_market_win_rate")
            # Treat anyone the previous run rated WEAK or above with >= 10 markets
            # as a "previous candidate" we want to chase.
            if entry.get("tier") in {"ELITE", "STRONG", "WATCH", "WEAK"} and sample >= 10:
                result[address] = {
                    "previous_sample": sample,
                    "previous_win_rate": win_rate,
                    "previous_tier": entry.get("tier"),
                }
        return result

    def _score_candidate(
        self,
        address: str,
        *,
        agg: WalletAggregate,
        discovery_agg: WalletAggregate | None,
        validation_agg: WalletAggregate | None,
        previous_payload: dict[str, Any] | None,
        markets_by_id: dict[str, ResolvedMarket],
        short_window_crypto_dominance: bool,
        survivor_bias: bool,
    ) -> CandidateRow:
        wins = len(agg.winning_markets)
        losses = len(agg.losing_markets)
        sample = wins + losses
        win_rate = round(wins / sample, 4) if sample else None

        disc_wins = len(discovery_agg.winning_markets) if discovery_agg else 0
        disc_losses = len(discovery_agg.losing_markets) if discovery_agg else 0
        discovery_sample = disc_wins + disc_losses
        discovery_win_rate = round(disc_wins / discovery_sample, 4) if discovery_sample else None

        val_wins = len(validation_agg.winning_markets) if validation_agg else 0
        val_losses = len(validation_agg.losing_markets) if validation_agg else 0
        validation_sample = val_wins + val_losses
        validation_win_rate = round(val_wins / validation_sample, 4) if validation_sample else None

        category_breakdown = self._category_breakdown(agg)
        top_category, category_share = MarketFirstDiscoveryService.detect_category_concentration(agg)
        correlation_flag, slug_counts = MarketFirstDiscoveryService.detect_correlated_markets(agg, markets_by_id)
        correlation_share = (
            max(slug_counts.values()) / max(sum(slug_counts.values()), 1) if slug_counts else 0.0
        )

        warnings: list[str] = []
        if short_window_crypto_dominance:
            warnings.append("global_short_window_crypto_dominance")
        if survivor_bias:
            warnings.append("global_survivor_bias_suspected")
        if win_rate is not None and win_rate >= 0.85 and sample < 30:
            warnings.append("high_win_rate_small_sample")
        if category_share >= 0.90 and sample >= 5:
            warnings.append(f"category_concentration_{int(category_share * 100)}pct_in_{top_category}")
        if correlation_flag:
            warnings.append("correlated_markets_single_event_slug")
        if validation_sample == 0 and sample >= 10:
            warnings.append("no_validation_partition_coverage")

        data_quality_warning = bool(warnings)

        candidate_status = self._derive_status(
            sample=sample,
            win_rate=win_rate,
            discovery_win_rate=discovery_win_rate,
            validation_sample=validation_sample,
            validation_win_rate=validation_win_rate,
            category_share=category_share,
            correlation_flag=correlation_flag,
            short_window_crypto_dominance=short_window_crypto_dominance,
        )

        final_recommendation = self._recommendation_text(candidate_status, sample, validation_sample)

        return CandidateRow(
            address=address,
            previous_sample=(previous_payload or {}).get("previous_sample"),
            previous_win_rate=(previous_payload or {}).get("previous_win_rate"),
            expanded_sample=sample,
            expanded_win_rate=win_rate,
            discovery_sample=discovery_sample,
            discovery_win_rate=discovery_win_rate,
            validation_sample=validation_sample,
            validation_win_rate=validation_win_rate,
            category_breakdown=category_breakdown,
            best_category=top_category,
            category_concentration_share=category_share,
            market_correlation_warning=correlation_flag,
            market_correlation_share=round(correlation_share, 4),
            data_quality_warning=data_quality_warning,
            candidate_status=candidate_status,
            final_recommendation=final_recommendation,
            warnings=warnings,
            data_source="polymarket_gamma+data",
        )

    def _category_breakdown(self, agg: WalletAggregate) -> dict[str, dict[str, Any]]:
        payload: dict[str, dict[str, Any]] = {}
        for category, stats in agg.category_results.items():
            wins = stats["wins"]
            losses = stats["losses"]
            sample = wins + losses
            payload[category] = {
                "wins": wins,
                "losses": losses,
                "sample": sample,
                "win_rate": round(wins / sample, 4) if sample else None,
            }
        return payload

    def _derive_status(
        self,
        *,
        sample: int,
        win_rate: float | None,
        discovery_win_rate: float | None,
        validation_sample: int,
        validation_win_rate: float | None,
        category_share: float,
        correlation_flag: bool,
        short_window_crypto_dominance: bool,
    ) -> CandidateStatus:
        # Bias filters trump everything: a wallet on a single event slug is
        # never promoted, regardless of how high its win rate is.
        if correlation_flag or category_share >= 0.95:
            return "BIASED_SAMPLE"
        if win_rate is None:
            return "DROPPED"
        if sample < 10:
            return "DROPPED"
        # Out-of-sample validation collapse → demoted, not promoted.
        if (
            sample >= 30
            and discovery_win_rate is not None
            and discovery_win_rate >= 0.70
            and validation_sample >= 5
            and validation_win_rate is not None
            and validation_win_rate < 0.50
        ):
            return "FAILED_VALIDATION"
        confidence = compute_win_rate_confidence(sample)

        # v0.5.3 strict ELITE rule:
        # - sample >= 100, confidence == HIGH (= sample >= 100 by construction)
        # - validation_sample >= 10
        # - validation_win_rate >= 0.70
        # - |discovery_win_rate - validation_win_rate| <= 0.15
        # - not short_window_crypto_dominated, not BIASED_SAMPLE / FAILED_VALIDATION
        # If the *only* thing missing is the gap or the validation_wr threshold
        # but the wallet is otherwise ELITE-eligible (sample 100+, HIGH conf),
        # we route it to OUTLIER_FLAGGED instead of ELITE — never silent demotion.
        elite_eligible_pre = (
            sample >= 100
            and confidence == "HIGH"
            and (win_rate or 0) >= 0.65
            and validation_sample >= 10
            and validation_win_rate is not None
            and not short_window_crypto_dominance
        )
        if elite_eligible_pre:
            gap = abs((discovery_win_rate or 0) - validation_win_rate) if discovery_win_rate is not None else None
            if validation_win_rate >= 0.70 and gap is not None and gap <= 0.15:
                return "ELITE"
            return "OUTLIER_FLAGGED"

        # STRONG bar: medium sample, holds within +/-0.20.
        if (
            sample >= 30
            and confidence in {"MEDIUM", "HIGH"}
            and (win_rate or 0) >= 0.65
            and validation_sample >= 5
            and validation_win_rate is not None
            and abs(win_rate - validation_win_rate) <= 0.20
            and validation_win_rate >= 0.55
        ):
            return "STRONG"
        # CANDIDATE_ELITE: high win rate, small sample → keep watching.
        if (win_rate or 0) >= 0.75 and 10 <= sample < 30:
            return "CANDIDATE_ELITE"
        # Sample large enough but win rate gone → no edge.
        if sample >= 30 and (win_rate or 0) < 0.55:
            return "DROPPED"
        if (win_rate or 0) >= 0.60:
            return "CANDIDATE_ELITE"
        return "DROPPED"

    def _recommendation_text(self, status: CandidateStatus, sample: int, validation_sample: int) -> str:
        if status == "ELITE":
            return "Promote to ELITE watchlist; copy with conservative size."
        if status == "STRONG":
            return "Add to STRONG watchlist; eligible for paper-trade auto with risk caps."
        if status == "CANDIDATE_ELITE":
            return f"Keep on candidate list; need sample ≥ 30 (currently {sample})."
        if status == "OUTLIER_FLAGGED":
            return (
                "Sample large enough but discovery↔validation gap > 15% or validation WR < 70% — "
                "do NOT include in SAFE/AGGRESSIVE/FULL_PAPER; observe separately."
            )
        if status == "BIASED_SAMPLE":
            return "Do not promote; sample looks correlated or single-category."
        if status == "FAILED_VALIDATION":
            return f"Drop — discovery edge collapsed in the {validation_sample}-market validation partition."
        return "No edge in this sample. Re-evaluate later with more markets."

    def _persist_statuses(self, rows: Iterable[CandidateRow]) -> None:
        """Push the validated status back onto MarketFirstWalletRecord so the
        risk engine can answer 'is this wallet a STRONG / CANDIDATE_ELITE?' in
        a single SQLite lookup."""
        for row in rows:
            record = self.session.get(MarketFirstWalletRecord, row.address)
            if record is None:
                continue
            record.candidate_status = row.candidate_status
            self.session.add(record)
        self.session.commit()

    def _write_csv(self, rows: Iterable[CandidateRow]) -> None:
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                payload = row.to_dict()
                payload["category_breakdown"] = json.dumps(payload["category_breakdown"]) if payload["category_breakdown"] else ""
                payload["warnings"] = ";".join(payload["warnings"])
                writer.writerow(payload)
