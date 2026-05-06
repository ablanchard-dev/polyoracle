"""Run a one-shot discovery audit and produce a JSON+CSV report.

The audit answers two questions:
  1. Is the wallet **discovery** source pointing us at real, exploitable smart
     wallets, or just at the loudest whales / mock data?
  2. Can we measure each audited wallet's **resolved-market win rate** with
     enough confidence to trust it as a sub-metric?

The output mirrors the v0.4.2 spec:
- A summary endpoint payload (discovery audit JSON).
- A CSV ranked top-N export with all SmartWalletScore sub-scores plus win-rate
  metrics and warnings.
- A high-level conclusion picked from a fixed enum:
    DISCOVERY_GOOD,
    DISCOVERY_VOLUME_BIASED,
    DISCOVERY_TOO_MUCH_MOCK,
    DISCOVERY_INSUFFICIENT_DATA,
    DISCOVERY_API_UNRELIABLE,
    DISCOVERY_NEEDS_ALTERNATIVE_METHOD.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Literal

from sqlmodel import Session

from app.config import get_settings
from app.services.smart_wallet_auditor import SmartWalletAuditor, WalletAuditResult
from app.services.win_rate_engine import WinRateBreakdown, WinRateEngine

logger = logging.getLogger(__name__)


DiscoveryConclusion = Literal[
    "DISCOVERY_GOOD",
    "DISCOVERY_VOLUME_BIASED",
    "DISCOVERY_TOO_MUCH_MOCK",
    "DISCOVERY_INSUFFICIENT_DATA",
    "DISCOVERY_API_UNRELIABLE",
    "DISCOVERY_NEEDS_ALTERNATIVE_METHOD",
]


CSV_COLUMNS = [
    "rank",
    "address",
    "final_score",
    "tier",
    "confidence",
    "pnl_score",
    "roi_score",
    "sample_size_score",
    "consistency_score",
    "copyability_score",
    "timing_score",
    "liquidity_quality_score",
    "risk_management_score",
    "resolved_market_win_rate",
    "trade_level_win_rate",
    "market_sample_size",
    "resolved_winning_markets",
    "resolved_losing_markets",
    "unresolved_markets_count",
    "win_rate_confidence",
    "volume_estimated",
    "trades_count",
    "markets_count",
    "category_specialization",
    "warnings",
    "reasons",
    "data_source",
]


@dataclass
class DiscoveryAuditReport:
    started_at: str
    finished_at: str
    duration_ms: float
    requested_limit: int
    data_source: str
    ranking_basis_detected: str
    discovery_errors: list[str]
    top_wallets_count: int
    audited_wallets_count: int
    failed_wallets_count: int
    tier_breakdown: dict[str, int]
    suspicious_count: int
    insufficient_data_count: int
    average_win_rate: float | None
    median_win_rate: float | None
    average_resolved_market_sample_size: float
    wallets_with_reliable_win_rate_count: int
    wallets_with_insufficient_win_rate_count: int
    warnings: list[str]
    conclusion: DiscoveryConclusion
    rationale: str
    csv_path: str
    json_path: str
    audited_wallets: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "requested_limit": self.requested_limit,
            "data_source": self.data_source,
            "ranking_basis_detected": self.ranking_basis_detected,
            "discovery_errors": list(self.discovery_errors),
            "top_wallets_count": self.top_wallets_count,
            "audited_wallets_count": self.audited_wallets_count,
            "failed_wallets_count": self.failed_wallets_count,
            "tier_breakdown": dict(self.tier_breakdown),
            "suspicious_count": self.suspicious_count,
            "insufficient_data_count": self.insufficient_data_count,
            "average_win_rate": self.average_win_rate,
            "median_win_rate": self.median_win_rate,
            "average_resolved_market_sample_size": self.average_resolved_market_sample_size,
            "wallets_with_reliable_win_rate_count": self.wallets_with_reliable_win_rate_count,
            "wallets_with_insufficient_win_rate_count": self.wallets_with_insufficient_win_rate_count,
            "warnings": list(self.warnings),
            "conclusion": self.conclusion,
            "rationale": self.rationale,
            "csv_path": self.csv_path,
            "json_path": self.json_path,
            "audited_wallets": list(self.audited_wallets),
        }


class DiscoveryAuditService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.auditor = SmartWalletAuditor(session)
        self.win_rate_engine = WinRateEngine(session)
        self.exports_dir = self.settings.resolved_sqlite_path.parent / "exports"
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    @property
    def csv_path(self) -> Path:
        return self.exports_dir / "wallet_discovery_top100.csv"

    @property
    def json_path(self) -> Path:
        return self.exports_dir / "wallet_discovery_audit.json"

    def latest_report(self) -> dict[str, Any] | None:
        if not self.json_path.exists():
            return None
        try:
            return json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read latest discovery audit report: %s", exc)
            return None

    def export_paths(self) -> dict[str, Any]:
        return {
            "exports_dir": str(self.exports_dir),
            "csv_path": str(self.csv_path),
            "csv_exists": self.csv_path.exists(),
            "json_path": str(self.json_path),
            "json_exists": self.json_path.exists(),
            "latest_report_summary": self._summary_from_disk(),
        }

    def _summary_from_disk(self) -> dict[str, Any] | None:
        report = self.latest_report()
        if report is None:
            return None
        return {
            key: report.get(key)
            for key in (
                "started_at",
                "data_source",
                "top_wallets_count",
                "audited_wallets_count",
                "failed_wallets_count",
                "tier_breakdown",
                "average_win_rate",
                "wallets_with_reliable_win_rate_count",
                "wallets_with_insufficient_win_rate_count",
                "conclusion",
            )
        }

    def run_audit(self, limit: int = 100, batch_size: int | None = None) -> DiscoveryAuditReport:
        started = datetime.now(UTC)
        batch_size = batch_size or 25

        discovery = self.auditor.discover_top_wallets_detailed(limit=limit)
        candidates = discovery["wallets"]
        data_source = discovery["data_source"]
        ranking_basis = discovery["ranking_basis"]
        discovery_errors = list(discovery.get("errors", []))

        audited: list[WalletAuditResult] = []
        failed: list[str] = []
        win_rate_payloads: dict[str, WinRateBreakdown] = {}
        # Audit in chunks so a single bad wallet does not abort the whole run.
        cache_lookup = {entry.get("address"): entry for entry in candidates}
        for index in range(0, len(candidates), batch_size):
            chunk = candidates[index : index + batch_size]
            for entry in chunk:
                address = entry.get("address")
                if not address:
                    continue
                # Compute win rate FIRST so it can feed the SmartWalletScore.
                wr: WinRateBreakdown | None
                try:
                    wr = self.win_rate_engine.compute_wallet_win_rate(address)
                except Exception as exc:
                    logger.warning("Win-rate compute failed for %s: %s", address, exc)
                    wr = None
                if wr is not None:
                    win_rate_payloads[address] = wr
                try:
                    audited.append(self.auditor.audit_wallet(address, entry, win_rate_breakdown=wr))
                except Exception as exc:
                    failed.append(f"{address}: {exc}")
                    logger.warning("Discovery audit failed for %s: %s", address, exc)

        tier_breakdown: dict[str, int] = {}
        for result in audited:
            tier_breakdown[result.tier] = tier_breakdown.get(result.tier, 0) + 1

        win_rates = [b.resolved_market_win_rate for b in win_rate_payloads.values() if b.resolved_market_win_rate is not None]
        sample_sizes = [b.market_sample_size for b in win_rate_payloads.values()]
        reliable_count = sum(1 for b in win_rate_payloads.values() if b.win_rate_confidence in {"MEDIUM", "HIGH"})
        insufficient_count = sum(1 for b in win_rate_payloads.values() if b.win_rate_confidence == "INSUFFICIENT_DATA")

        avg_win_rate = round(sum(win_rates) / len(win_rates), 4) if win_rates else None
        median_win_rate = round(median(win_rates), 4) if win_rates else None
        avg_sample_size = round(sum(sample_sizes) / len(sample_sizes), 2) if sample_sizes else 0.0

        warnings = self._compose_warnings(
            data_source=data_source,
            audited=audited,
            win_rate_payloads=win_rate_payloads,
            ranking_basis=ranking_basis,
            errors=discovery_errors,
        )
        conclusion, rationale = self._derive_conclusion(
            data_source=data_source,
            audited=audited,
            tier_breakdown=tier_breakdown,
            insufficient_count=insufficient_count,
            reliable_count=reliable_count,
            warnings=warnings,
            errors=discovery_errors,
        )

        # Build CSV rows (rank by smart_score desc).
        ranked = sorted(audited, key=lambda r: r.smart_score, reverse=True)
        csv_rows: list[dict[str, Any]] = []
        for rank, result in enumerate(ranked, start=1):
            wr = win_rate_payloads.get(result.address)
            entry = cache_lookup.get(result.address, {})
            csv_rows.append(
                {
                    "rank": rank,
                    "address": result.address,
                    "final_score": result.smart_score,
                    "tier": result.tier,
                    "confidence": result.confidence,
                    "pnl_score": result.breakdown.pnl_score,
                    "roi_score": result.breakdown.roi_score,
                    "sample_size_score": result.breakdown.sample_size_score,
                    "consistency_score": result.breakdown.consistency_score,
                    "copyability_score": result.breakdown.copyability_score,
                    "timing_score": result.breakdown.timing_score,
                    "liquidity_quality_score": result.breakdown.liquidity_quality_score,
                    "risk_management_score": result.breakdown.risk_management_score,
                    "resolved_market_win_rate": wr.resolved_market_win_rate if wr else None,
                    "trade_level_win_rate": wr.trade_level_win_rate if wr else None,
                    "market_sample_size": wr.market_sample_size if wr else 0,
                    "resolved_winning_markets": wr.resolved_winning_markets if wr else 0,
                    "resolved_losing_markets": wr.resolved_losing_markets if wr else 0,
                    "unresolved_markets_count": wr.unresolved_markets_count if wr else 0,
                    "win_rate_confidence": wr.win_rate_confidence if wr else "INSUFFICIENT_DATA",
                    "volume_estimated": entry.get("volume", 0.0),
                    "trades_count": entry.get("trades_observed", 0),
                    "markets_count": result.sample_size,
                    "category_specialization": result.specialty or "",
                    "warnings": ";".join((wr.win_rate_warnings if wr else []) + result.notes),
                    "reasons": result.suspicious_reason or "",
                    "data_source": result.data_source,
                }
            )

        json_payload_wallets = []
        for row in csv_rows:
            payload = dict(row)
            payload["warnings"] = [chunk for chunk in payload["warnings"].split(";") if chunk]
            json_payload_wallets.append(payload)

        finished = datetime.now(UTC)
        duration_ms = round((finished - started).total_seconds() * 1000, 2)

        report = DiscoveryAuditReport(
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_ms=duration_ms,
            requested_limit=limit,
            data_source=data_source,
            ranking_basis_detected=ranking_basis,
            discovery_errors=discovery_errors,
            top_wallets_count=len(candidates),
            audited_wallets_count=len(audited),
            failed_wallets_count=len(failed),
            tier_breakdown=tier_breakdown,
            suspicious_count=sum(1 for r in audited if r.suspicious),
            insufficient_data_count=sum(1 for r in audited if r.tier == "INSUFFICIENT_DATA"),
            average_win_rate=avg_win_rate,
            median_win_rate=median_win_rate,
            average_resolved_market_sample_size=avg_sample_size,
            wallets_with_reliable_win_rate_count=reliable_count,
            wallets_with_insufficient_win_rate_count=insufficient_count,
            warnings=warnings,
            conclusion=conclusion,
            rationale=rationale,
            csv_path=str(self.csv_path),
            json_path=str(self.json_path),
            audited_wallets=json_payload_wallets,
        )
        self._write_csv(csv_rows)
        self._write_json(report)
        return report

    # ---------------- internals ----------------

    def _write_csv(self, rows: list[dict[str, Any]]) -> None:
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _write_json(self, report: DiscoveryAuditReport) -> None:
        self.json_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")

    def _compose_warnings(
        self,
        *,
        data_source: str,
        audited: list[WalletAuditResult],
        win_rate_payloads: dict[str, WinRateBreakdown],
        ranking_basis: str,
        errors: list[str],
    ) -> list[str]:
        warnings: list[str] = []
        if data_source == "unavailable":
            warnings.append("api_unavailable_no_mock_fallback")
        if data_source.startswith("mock"):
            warnings.append("data_source_is_mock")
        if "leaderboard_failed" in " ".join(errors):
            warnings.append("legacy_leaderboard_endpoint_404_in_production")
        if not audited:
            warnings.append("no_wallets_audited")
        suspicious_share = sum(1 for r in audited if r.suspicious) / max(len(audited), 1)
        if suspicious_share > 0.20:
            warnings.append("more_than_20pct_suspicious_wallets")
        insufficient_share = sum(1 for r in audited if r.tier == "INSUFFICIENT_DATA") / max(len(audited), 1)
        if insufficient_share > 0.50:
            warnings.append("over_half_audited_have_insufficient_data")
        if win_rate_payloads:
            confidences = [b.win_rate_confidence for b in win_rate_payloads.values()]
            if confidences.count("INSUFFICIENT_DATA") / len(confidences) > 0.80:
                warnings.append("over_80pct_have_insufficient_win_rate_sample")
        if ranking_basis == "recent_trade_notional":
            warnings.append("ranking_is_volume_proxy_not_pnl")
        return warnings

    def _derive_conclusion(
        self,
        *,
        data_source: str,
        audited: list[WalletAuditResult],
        tier_breakdown: dict[str, int],
        insufficient_count: int,
        reliable_count: int,
        warnings: list[str],
        errors: list[str],
    ) -> tuple[DiscoveryConclusion, str]:
        if data_source == "unavailable":
            return (
                "DISCOVERY_API_UNRELIABLE",
                "Public Polymarket APIs returned no usable data and mock fallback is disabled.",
            )
        if data_source.startswith("mock"):
            return (
                "DISCOVERY_TOO_MUCH_MOCK",
                "Discovery is running on the mock fallback. Set POLYMARKET_PUBLIC_ENABLED=true and verify the network can reach data-api.polymarket.com.",
            )
        if not audited:
            return (
                "DISCOVERY_INSUFFICIENT_DATA",
                "Discovery returned candidates but none could be audited.",
            )
        elite_strong = tier_breakdown.get("ELITE", 0) + tier_breakdown.get("STRONG", 0)
        share_strong = elite_strong / max(len(audited), 1)
        share_insufficient = insufficient_count / max(len(audited), 1)
        if "ranking_is_volume_proxy_not_pnl" in warnings and share_strong < 0.10:
            return (
                "DISCOVERY_VOLUME_BIASED",
                "Discovery ranks by recent trade notional. Top wallets are mostly high-volume traders, "
                "not necessarily skilled ones. Add a market-first or ROI-first discovery before relying on the top 100.",
            )
        if share_insufficient > 0.60:
            return (
                "DISCOVERY_INSUFFICIENT_DATA",
                "More than 60% of audited wallets have INSUFFICIENT_DATA — Data API does not expose enough history to score them reliably.",
            )
        if share_strong >= 0.10 and reliable_count >= 5:
            return (
                "DISCOVERY_GOOD",
                "Discovery returned a believable mix of strong wallets and a meaningful number have reliable resolved-market win rates.",
            )
        return (
            "DISCOVERY_NEEDS_ALTERNATIVE_METHOD",
            "Current path returns wallets but the SmartWalletScore + win-rate signal is too weak. Build a market-first / ROI-first alternative discovery to complement it.",
        )
