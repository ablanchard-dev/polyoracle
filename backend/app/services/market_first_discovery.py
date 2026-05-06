"""Market-first wallet discovery.

Empirical pipeline answering one question:
"Starting from markets that have actually resolved, which wallets keep
holding the winning outcome?"

Steps:
1. ``MarketResolutionScanner`` walks Gamma's closed markets and produces a
   stream of ``ResolvedMarket`` objects (usable / rejected with reason code).
2. For each usable market we hit ``/trades?market={conditionId}`` and reduce
   the trades into per-wallet, per-outcome positions.
3. A wallet that was net long the winning outcome at resolution counts as a
   *win* on that market; net long the losing outcome counts as a *loss*; net
   short or scalp / flat counts as *unresolved* and never moves the win rate.
4. We aggregate across all audited markets, run the scoring + tier rules,
   compute a ``MarketFirstWalletScore`` (0-100) and a composite score that
   mixes historical skill with current activity, and persist + export.

The whole thing is local-first, never invents a win/loss, and surfaces a
``MARKET_FIRST_*`` conclusion that says — out loud — whether the discovery
actually found smart wallets or whether the data was too thin.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Literal

from sqlmodel import Session, select

from app.config import get_settings
from app.models.wallet import MarketFirstWalletRecord, ResolvedMarketRecord
from app.services.market_resolution_scanner import MarketResolutionScanner, ResolvedMarket
from app.services.polymarket.data_client import DataClient
from app.services.polymarket.normalizers import safe_float
from app.services.smart_wallet_auditor import SmartWalletAuditor, _normalize_address
from app.services.win_rate_engine import compute_win_rate_confidence

logger = logging.getLogger(__name__)


MarketFirstConclusion = Literal[
    "MARKET_FIRST_GOOD",
    "MARKET_FIRST_DISCOVERY_READY",
    "MARKET_FIRST_INSUFFICIENT_DATA",
    "MARKET_FIRST_API_LIMITED",
    "MARKET_FIRST_NEEDS_MORE_MARKETS",
    "MARKET_FIRST_NO_ELITE_FOUND",
]


@dataclass
class WalletAggregate:
    address: str
    winning_markets: set[str] = field(default_factory=set)
    losing_markets: set[str] = field(default_factory=set)
    unresolved_markets: set[str] = field(default_factory=set)
    notional_total: float = 0.0
    notional_per_market: list[float] = field(default_factory=list)
    last_trade_at: int | None = None
    trades_count: int = 0
    category_results: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"wins": 0, "losses": 0}))


@dataclass
class WalletDiscoveryRow:
    address: str
    market_first_score: float
    composite_score: float
    tier: str
    status: str
    resolved_market_win_rate: float | None
    win_rate_confidence: str
    resolved_markets_traded: int
    resolved_winning_markets: int
    resolved_losing_markets: int
    unresolved_markets_count: int
    best_category: str | None
    category_win_rates: dict[str, dict[str, Any]]
    recent_activity_score: float
    copyability_score: float
    total_resolved_notional: float
    average_position_size: float
    median_position_size: float
    warnings: list[str]
    reasons: list[str]
    data_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "market_first_score": self.market_first_score,
            "composite_score": self.composite_score,
            "tier": self.tier,
            "status": self.status,
            "resolved_market_win_rate": self.resolved_market_win_rate,
            "win_rate_confidence": self.win_rate_confidence,
            "resolved_markets_traded": self.resolved_markets_traded,
            "resolved_winning_markets": self.resolved_winning_markets,
            "resolved_losing_markets": self.resolved_losing_markets,
            "unresolved_markets_count": self.unresolved_markets_count,
            "best_category": self.best_category,
            "category_win_rates": self.category_win_rates,
            "recent_activity_score": self.recent_activity_score,
            "copyability_score": self.copyability_score,
            "total_resolved_notional": self.total_resolved_notional,
            "average_position_size": self.average_position_size,
            "median_position_size": self.median_position_size,
            "warnings": list(self.warnings),
            "reasons": list(self.reasons),
            "data_source": self.data_source,
        }


@dataclass
class MarketFirstReport:
    started_at: str
    finished_at: str
    duration_ms: float
    days_back: int
    markets_scanned: int
    markets_usable: int
    markets_rejected: int
    rejection_reasons: dict[str, int]
    wallets_discovered: int
    wallets_with_medium_high_confidence: int
    tier_breakdown: dict[str, int]
    status_breakdown: dict[str, int]
    average_win_rate: float | None
    median_win_rate: float | None
    average_resolved_market_sample_size: float
    api_errors: int
    warnings: list[str]
    conclusion: MarketFirstConclusion
    rationale: str
    data_source: str
    csv_paths: dict[str, str]
    json_path: str
    top_wallets: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "days_back": self.days_back,
            "markets_scanned": self.markets_scanned,
            "markets_usable": self.markets_usable,
            "markets_rejected": self.markets_rejected,
            "rejection_reasons": dict(self.rejection_reasons),
            "wallets_discovered": self.wallets_discovered,
            "wallets_with_medium_high_confidence": self.wallets_with_medium_high_confidence,
            "tier_breakdown": dict(self.tier_breakdown),
            "status_breakdown": dict(self.status_breakdown),
            "average_win_rate": self.average_win_rate,
            "median_win_rate": self.median_win_rate,
            "average_resolved_market_sample_size": self.average_resolved_market_sample_size,
            "api_errors": self.api_errors,
            "warnings": list(self.warnings),
            "conclusion": self.conclusion,
            "rationale": self.rationale,
            "data_source": self.data_source,
            "csv_paths": dict(self.csv_paths),
            "json_path": self.json_path,
            "top_wallets": list(self.top_wallets),
        }


WALLETS_CSV_COLUMNS = [
    "rank",
    "address",
    "market_first_score",
    "composite_score",
    "tier",
    "status",
    "resolved_market_win_rate",
    "win_rate_confidence",
    "resolved_markets_traded",
    "resolved_winning_markets",
    "resolved_losing_markets",
    "unresolved_markets_count",
    "best_category",
    "category_win_rates",
    "recent_activity_score",
    "copyability_score",
    "total_resolved_notional",
    "average_position_size",
    "median_position_size",
    "warnings",
    "reasons",
    "data_source",
]
MARKETS_CSV_COLUMNS = [
    "market_id",
    "condition_id",
    "question",
    "category",
    "end_date",
    "closed",
    "winning_outcome",
    "holders_count",
    "trades_count",
    "usable",
    "rejection_reason",
]
REJECTED_CSV_COLUMNS = ["market_id", "condition_id", "question", "category", "end_date", "rejection_reason"]


class MarketFirstDiscoveryService:
    def __init__(self, session: Session, *, export_suffix: str = "") -> None:
        self.session = session
        self.settings = get_settings()
        self.scanner = MarketResolutionScanner()
        self.exports_dir = self.settings.resolved_sqlite_path.parent / "exports"
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self._data_client: DataClient | None = None
        self._sleep_seconds = max(0.0, self.settings.market_first_sleep_ms_between_calls / 1000.0)
        self._export_suffix = (f"_{export_suffix.strip('_')}" if export_suffix else "")

    # ---------------- public API ----------------

    @property
    def json_path(self) -> Path:
        return self.exports_dir / f"market_first_discovery_report{self._export_suffix}.json"

    @property
    def diagnostics_path(self) -> Path:
        return self.exports_dir / f"market_first_diagnostics{self._export_suffix}.json"

    @property
    def wallets_csv_path(self) -> Path:
        return self.exports_dir / f"market_first_wallets{self._export_suffix}.csv"

    @property
    def markets_csv_path(self) -> Path:
        return self.exports_dir / f"market_first_markets{self._export_suffix}.csv"

    @property
    def rejected_csv_path(self) -> Path:
        return self.exports_dir / f"market_first_rejected_markets{self._export_suffix}.csv"

    def export_paths(self) -> dict[str, Any]:
        return {
            "exports_dir": str(self.exports_dir),
            "json_path": str(self.json_path),
            "json_exists": self.json_path.exists(),
            "wallets_csv_path": str(self.wallets_csv_path),
            "wallets_csv_exists": self.wallets_csv_path.exists(),
            "markets_csv_path": str(self.markets_csv_path),
            "markets_csv_exists": self.markets_csv_path.exists(),
            "rejected_csv_path": str(self.rejected_csv_path),
            "rejected_csv_exists": self.rejected_csv_path.exists(),
        }

    def latest_report(self) -> dict[str, Any] | None:
        if not self.json_path.exists():
            return None
        try:
            return json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read latest market-first report: %s", exc)
            return None

    def status(self) -> dict[str, Any]:
        latest = self.latest_report()
        return {
            "available": latest is not None,
            "exports": self.export_paths(),
            "summary": (
                {
                    key: latest.get(key)
                    for key in (
                        "started_at",
                        "duration_ms",
                        "days_back",
                        "markets_scanned",
                        "markets_usable",
                        "markets_rejected",
                        "wallets_discovered",
                        "wallets_with_medium_high_confidence",
                        "tier_breakdown",
                        "status_breakdown",
                        "average_win_rate",
                        "data_source",
                        "conclusion",
                    )
                }
                if latest
                else None
            ),
        }

    # ---------------- run ----------------

    def run(
        self,
        *,
        days_back: int | None = None,
        max_markets: int | None = None,
        trades_per_market: int | None = None,
    ) -> MarketFirstReport:
        if not self.settings.market_first_discovery_enabled:
            raise RuntimeError("MARKET_FIRST_DISCOVERY_ENABLED is false")
        days_back = days_back or self.settings.market_first_days_back
        max_markets = max_markets or self.settings.market_first_max_markets_per_run
        trades_limit = trades_per_market or self.settings.market_first_trades_per_market_limit

        started = datetime.now(UTC)

        all_markets, usable_markets, rejected_markets, trades_by_market, api_errors = self._scan_and_fetch(
            days_back=days_back,
            max_markets=max_markets,
            trades_limit=trades_limit,
        )
        market_trade_counts = {mid: len(trades) for mid, trades in trades_by_market.items()}

        # 2. Aggregate every wallet across the full set.
        aggregates: dict[str, WalletAggregate] = {}
        for market in usable_markets:
            trades = trades_by_market.get(market.market_id, [])
            self._aggregate_market_into_wallets(market, trades, aggregates)

        # 3. Score every wallet we saw, attach status, persist.
        rows = self._score_wallets(aggregates, usable_markets)
        rows.sort(key=lambda r: r.market_first_score, reverse=True)
        self._persist(rows, usable_markets, rejected_markets, market_trade_counts)

        # 4. Reporting.
        finished = datetime.now(UTC)
        duration_ms = round((finished - started).total_seconds() * 1000, 2)
        rejection_reasons = Counter(m.rejection_reason() or "LOW_DATA_QUALITY" for m in rejected_markets)
        win_rates = [r.resolved_market_win_rate for r in rows if r.resolved_market_win_rate is not None]
        sample_sizes = [r.resolved_markets_traded for r in rows]
        tier_breakdown = Counter(r.tier for r in rows)
        status_breakdown = Counter(r.status for r in rows)
        wallets_with_confidence = sum(
            1 for r in rows if r.win_rate_confidence in {"MEDIUM", "HIGH"}
        )

        warnings = self._compose_warnings(
            usable_markets=usable_markets,
            rows=rows,
            api_errors=api_errors,
        )
        conclusion, rationale = self._derive_conclusion(
            usable_markets=usable_markets,
            rows=rows,
            api_errors=api_errors,
            tier_breakdown=tier_breakdown,
            wallets_with_confidence=wallets_with_confidence,
        )

        # 5. Exports + diagnostics.
        self._write_exports(rows, usable_markets, rejected_markets, market_trade_counts)
        self._write_diagnostics(usable_markets, trades_by_market, rows)

        report = MarketFirstReport(
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_ms=duration_ms,
            days_back=days_back,
            markets_scanned=len(all_markets),
            markets_usable=len(usable_markets),
            markets_rejected=len(rejected_markets),
            rejection_reasons=dict(rejection_reasons),
            wallets_discovered=len(rows),
            wallets_with_medium_high_confidence=wallets_with_confidence,
            tier_breakdown=dict(tier_breakdown),
            status_breakdown=dict(status_breakdown),
            average_win_rate=round(sum(win_rates) / len(win_rates), 4) if win_rates else None,
            median_win_rate=round(median(win_rates), 4) if win_rates else None,
            average_resolved_market_sample_size=round(sum(sample_sizes) / len(sample_sizes), 2) if sample_sizes else 0.0,
            api_errors=api_errors,
            warnings=warnings,
            conclusion=conclusion,
            rationale=rationale,
            data_source="polymarket_gamma+data" if not self.settings.polymarket_public_enabled else "polymarket_gamma+data",
            csv_paths={
                "wallets": str(self.wallets_csv_path),
                "markets": str(self.markets_csv_path),
                "rejected": str(self.rejected_csv_path),
            },
            json_path=str(self.json_path),
            top_wallets=[row.to_dict() for row in rows[:50]],
        )
        self.json_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        logger.info(
            "market_first finished: scanned=%d usable=%d rejected=%d wallets=%d conclusion=%s",
            len(all_markets),
            len(usable_markets),
            len(rejected_markets),
            len(rows),
            conclusion,
        )
        return report

    # ---------------- listing ----------------

    def list_top_wallets(self, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            select(MarketFirstWalletRecord)
            .where(MarketFirstWalletRecord.tier != "INSUFFICIENT_DATA")
            .order_by(MarketFirstWalletRecord.market_first_score.desc())
            .limit(limit)
        )
        rows = list(self.session.exec(statement).all())
        if not rows:
            statement = (
                select(MarketFirstWalletRecord)
                .order_by(MarketFirstWalletRecord.market_first_score.desc())
                .limit(limit)
            )
            rows = list(self.session.exec(statement).all())
        return [self._record_to_dict(record) for record in rows]

    def get_wallet(self, address: str) -> dict[str, Any] | None:
        record = self.session.get(MarketFirstWalletRecord, _normalize_address(address))
        return self._record_to_dict(record) if record else None

    def list_markets(self, usable_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        statement = select(ResolvedMarketRecord).order_by(ResolvedMarketRecord.audited_at.desc()).limit(limit)
        rows = [
            self._market_record_to_dict(record)
            for record in self.session.exec(statement).all()
        ]
        if usable_only:
            rows = [row for row in rows if row["usable"]]
        return rows

    def list_rejected_markets(self, limit: int = 200) -> list[dict[str, Any]]:
        statement = (
            select(ResolvedMarketRecord)
            .where(ResolvedMarketRecord.usable == False)  # noqa: E712 - SQL boolean comparison
            .order_by(ResolvedMarketRecord.audited_at.desc())
            .limit(limit)
        )
        return [self._market_record_to_dict(record) for record in self.session.exec(statement).all()]

    def reclassify_existing_records(self) -> dict[str, int]:
        """Re-apply the current tier rules to every persisted ``MarketFirstWalletRecord``
        and rewrite the JSON+CSV exports under the active suffix. Used when a
        scoring rule changes (e.g. v0.5.2 strict ELITE) so we don't have to
        re-fetch trades for thousands of markets just to recount tiers.
        """
        from collections import Counter as _Counter

        records = list(self.session.exec(select(MarketFirstWalletRecord)).all())
        before = _Counter(r.tier for r in records)
        for record in records:
            new_tier = self._classify_tier(
                record.market_first_score,
                record.win_rate_confidence,
                record.resolved_markets_traded,
            )
            record.tier = new_tier
            record.status = self._derive_status(
                new_tier,
                record.recent_activity_score,
                record.resolved_markets_traded,
                record.win_rate_confidence,
            )
            self.session.add(record)
        self.session.commit()
        after = _Counter(r.tier for r in records)
        # Re-write the JSON + CSV exports so downstream consumers see the new tiers.
        rows: list[WalletDiscoveryRow] = []
        for record in records:
            try:
                category_payload = json.loads(record.category_win_rates) if record.category_win_rates else {}
            except Exception:
                category_payload = {}
            try:
                warnings = json.loads(record.warnings) if record.warnings else []
            except Exception:
                warnings = []
            try:
                reasons = json.loads(record.reasons) if record.reasons else []
            except Exception:
                reasons = []
            rows.append(
                WalletDiscoveryRow(
                    address=record.address,
                    market_first_score=record.market_first_score,
                    composite_score=record.composite_score,
                    tier=record.tier,
                    status=record.status,
                    resolved_market_win_rate=record.resolved_market_win_rate,
                    win_rate_confidence=record.win_rate_confidence,
                    resolved_markets_traded=record.resolved_markets_traded,
                    resolved_winning_markets=record.resolved_winning_markets,
                    resolved_losing_markets=record.resolved_losing_markets,
                    unresolved_markets_count=0,
                    best_category=record.best_category,
                    category_win_rates=category_payload,
                    recent_activity_score=record.recent_activity_score,
                    copyability_score=record.copyability_score,
                    total_resolved_notional=record.total_resolved_notional,
                    average_position_size=record.average_position_size,
                    median_position_size=record.median_position_size,
                    warnings=warnings,
                    reasons=reasons,
                    data_source=record.data_source,
                )
            )
        rows.sort(key=lambda r: r.market_first_score, reverse=True)
        # Re-load the cached JSON report and patch the tier breakdown / top wallets.
        report_payload: dict[str, Any] = {}
        if self.json_path.exists():
            try:
                report_payload = json.loads(self.json_path.read_text(encoding="utf-8"))
            except Exception:
                report_payload = {}
        report_payload["tier_breakdown"] = dict(after)
        report_payload["top_wallets"] = [r.to_dict() for r in rows[:50]]
        report_payload["wallets_discovered"] = len(rows)
        report_payload["reclassified_at"] = datetime.now(UTC).isoformat()
        self.json_path.write_text(json.dumps(report_payload, indent=2, default=str), encoding="utf-8")
        # Re-write the wallets CSV (markets/rejected unchanged).
        with self.wallets_csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=WALLETS_CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for rank, row in enumerate(rows, start=1):
                payload = row.to_dict()
                payload["rank"] = rank
                payload["category_win_rates"] = json.dumps(payload["category_win_rates"]) if payload["category_win_rates"] else ""
                payload["warnings"] = ";".join(payload["warnings"])
                payload["reasons"] = ";".join(payload["reasons"])
                writer.writerow(payload)
        return {
            "before": dict(before),
            "after": dict(after),
            "records": len(records),
        }

    def category_winrate(self, address: str) -> dict[str, Any]:
        record = self.session.get(MarketFirstWalletRecord, _normalize_address(address))
        if record is None:
            return {"address": address, "available": False}
        try:
            payload = json.loads(record.category_win_rates) if record.category_win_rates else {}
        except Exception:
            payload = {}
        return {
            "address": record.address,
            "available": True,
            "best_category": record.best_category,
            "category_win_rates": payload,
        }

    # ---------------- internals: aggregation ----------------

    def _data(self) -> DataClient:
        if self._data_client is None:
            self._data_client = DataClient()
        return self._data_client

    def _fetch_market_trades(self, condition_id: str, limit: int) -> list[dict[str, Any]]:
        if not condition_id:
            return []
        if not self.settings.polymarket_public_enabled:
            return []
        return self._data().fetch_market_trades(condition_id, limit=limit)

    def _scan_and_fetch(
        self,
        *,
        days_back: int,
        max_markets: int,
        trades_limit: int,
    ) -> tuple[list[ResolvedMarket], list[ResolvedMarket], list[ResolvedMarket], dict[str, list[dict[str, Any]]], int]:
        """Shared primitive: scan markets + fetch trades once. Returns the full
        lists and the trade map so callers can aggregate as many times as they
        want without paying the network cost twice.
        """
        all_markets: list[ResolvedMarket] = []
        try:
            scan_limit = max(max_markets * 2, self.settings.market_first_market_limit)
            all_markets = self.scanner.fetch_resolved_markets(days_back=days_back, limit=scan_limit)
        except Exception as exc:
            logger.warning("Market resolution scan failed: %s", exc)

        usable_markets: list[ResolvedMarket] = []
        rejected_markets: list[ResolvedMarket] = []
        for market in all_markets:
            if market.is_usable:
                usable_markets.append(market)
            else:
                rejected_markets.append(market)
        usable_markets = usable_markets[:max_markets]

        trades_by_market: dict[str, list[dict[str, Any]]] = {}
        api_errors = 0
        for index, market in enumerate(usable_markets):
            try:
                trades_by_market[market.market_id] = self._fetch_market_trades(
                    market.condition_id or "", limit=trades_limit
                )
            except Exception as exc:
                api_errors += 1
                trades_by_market[market.market_id] = []
                logger.warning("Trade fetch failed for %s: %s", market.condition_id, exc)
            if self._sleep_seconds and index < len(usable_markets) - 1:
                time.sleep(self._sleep_seconds)
        return all_markets, usable_markets, rejected_markets, trades_by_market, api_errors

    def run_with_temporal_split(
        self,
        *,
        days_back: int | None = None,
        max_markets: int | None = None,
        trades_per_market: int | None = None,
        split_ratio: float = 0.7,
    ) -> dict[str, Any]:
        """Run the same scan as ``run`` but produce three separate per-wallet
        aggregates: full set, oldest-70% (discovery), newest-30% (validation).

        Markets are split by ``end_date`` ascending so the validation set is
        strictly more recent than the discovery set — that's what makes this an
        out-of-sample check rather than a random subset.
        """
        if not self.settings.market_first_discovery_enabled:
            raise RuntimeError("MARKET_FIRST_DISCOVERY_ENABLED is false")
        days_back = days_back or self.settings.market_first_days_back
        max_markets = max_markets or self.settings.market_first_max_markets_per_run
        trades_limit = trades_per_market or self.settings.market_first_trades_per_market_limit

        all_markets, usable_markets, rejected_markets, trades_by_market, api_errors = self._scan_and_fetch(
            days_back=days_back,
            max_markets=max_markets,
            trades_limit=trades_limit,
        )
        sorted_markets = sorted(usable_markets, key=lambda m: m.end_date or "")
        split_idx = max(1, min(len(sorted_markets) - 1, int(len(sorted_markets) * split_ratio)))
        discovery_markets = sorted_markets[:split_idx]
        validation_markets = sorted_markets[split_idx:]

        full_agg: dict[str, WalletAggregate] = {}
        discovery_agg: dict[str, WalletAggregate] = {}
        validation_agg: dict[str, WalletAggregate] = {}
        for market in usable_markets:
            trades = trades_by_market.get(market.market_id, [])
            self._aggregate_market_into_wallets(market, trades, full_agg)
        for market in discovery_markets:
            self._aggregate_market_into_wallets(market, trades_by_market.get(market.market_id, []), discovery_agg)
        for market in validation_markets:
            self._aggregate_market_into_wallets(market, trades_by_market.get(market.market_id, []), validation_agg)

        return {
            "all_markets": all_markets,
            "usable_markets": usable_markets,
            "rejected_markets": rejected_markets,
            "discovery_markets": discovery_markets,
            "validation_markets": validation_markets,
            "trades_by_market": trades_by_market,
            "full_agg": full_agg,
            "discovery_agg": discovery_agg,
            "validation_agg": validation_agg,
            "api_errors": api_errors,
        }

    # ---------------- internals: anti-bias ----------------

    @staticmethod
    def detect_short_window_crypto_dominance(usable_markets: list[ResolvedMarket]) -> tuple[bool, float]:
        """5-minute / 10-minute BTC-up-or-down style markets dominate Polymarket's
        recent closed-feed. They resolve in minutes and are basically coinflips,
        so a wallet that spams them can rack up a 90% win rate on a tiny sample.
        Flag the run when more than half of usable markets match the pattern."""
        if not usable_markets:
            return False, 0.0
        matched = 0
        for market in usable_markets:
            text = " ".join(filter(None, [market.question or "", market.slug or "", market.event_slug or ""])).lower()
            if any(keyword in text for keyword in ("up or down", "up/down", "updown", "5-min", "5min", "minute")):
                matched += 1
        share = matched / max(len(usable_markets), 1)
        return share >= 0.5, round(share, 4)

    @staticmethod
    def detect_loser_presence(
        full_agg: dict[str, WalletAggregate],
        usable_markets: list[ResolvedMarket],
    ) -> tuple[bool, float]:
        """Sanity check that we are not seeing a survivor-only feed: at least
        some wallets must be net-long the LOSING outcome on at least some
        markets. Returns ``(survivor_bias_flag, fraction_of_markets_with_losers)``.
        """
        market_ids = {m.market_id for m in usable_markets}
        markets_with_losers: set[str] = set()
        for agg in full_agg.values():
            markets_with_losers.update(mid for mid in agg.losing_markets if mid in market_ids)
        share = len(markets_with_losers) / max(len(market_ids), 1)
        return share < 0.30, round(share, 4)

    @staticmethod
    def detect_category_concentration(agg: WalletAggregate) -> tuple[str | None, float]:
        """Return the dominant category for a wallet and its share. Anything
        above 90% is suspicious because the wallet's edge cannot be
        distinguished from a single-category coin flip.

        We deliberately skip this signal when the dominant bucket is
        ``Uncategorised`` (Gamma leaves ``category`` unset on most closed
        markets in production), otherwise EVERYONE looks concentrated.
        """
        if not agg.category_results:
            return None, 0.0
        totals = {cat: stats["wins"] + stats["losses"] for cat, stats in agg.category_results.items()}
        total = sum(totals.values()) or 1
        top_cat, top_count = max(totals.items(), key=lambda item: item[1])
        share = top_count / total
        if top_cat in {None, "Uncategorised", "uncategorised", "unknown"}:
            return top_cat, 0.0
        return top_cat, round(share, 4)

    @staticmethod
    def _correlation_key(market: ResolvedMarket) -> str:
        """Group keys for the correlated-markets check.

        Many Polymarket families ship as a series of slugs that differ only by
        threshold (``espresso-fdv-above-100m...``, ``espresso-fdv-above-200m...``,
        ``espresso-fdv-above-700m...``). A wallet that "wins" all of those in
        one event is making one correlated bet, not several. So we collapse
        the slug to its first 4 hyphen-separated tokens before grouping.
        """
        if market.event_slug:
            return f"event::{market.event_slug}"
        slug = market.slug or ""
        if slug:
            tokens = slug.split("-")[:4]
            if tokens:
                return f"slug::{'-'.join(tokens)}"
        return f"market::{market.market_id}"

    @staticmethod
    def detect_correlated_markets(
        agg: WalletAggregate,
        markets_by_id: dict[str, ResolvedMarket],
    ) -> tuple[bool, dict[str, int]]:
        """Group a wallet's winning markets by event/slug-prefix. If 80%+ of
        wins land on the same correlated cluster the win rate is one bet, not
        many. Requires at least 5 winning markets so we don't penalise small
        samples that are anyway gated by the CANDIDATE_ELITE rule."""
        slug_counts: dict[str, int] = {}
        for market_id in agg.winning_markets:
            market = markets_by_id.get(market_id)
            if market is None:
                continue
            slug_counts[MarketFirstDiscoveryService._correlation_key(market)] = (
                slug_counts.get(MarketFirstDiscoveryService._correlation_key(market), 0) + 1
            )
        if not slug_counts:
            return False, {}
        total = sum(slug_counts.values())
        top_share = max(slug_counts.values()) / max(total, 1)
        # A real specialist on one event cluster needs ≥ 5 wins to be flagged.
        flag = top_share >= 0.80 and len(agg.winning_markets) >= 5
        return flag, slug_counts

    def _aggregate_market_into_wallets(
        self,
        market: ResolvedMarket,
        trades: list[dict[str, Any]],
        aggregates: dict[str, WalletAggregate],
    ) -> None:
        # Net buys/sells per (wallet, outcomeIndex) bucket.
        buckets: dict[tuple[str, int], dict[str, float]] = defaultdict(
            lambda: {"buys": 0.0, "sells": 0.0, "buys_notional": 0.0, "sells_notional": 0.0, "trade_count": 0, "last_ts": 0}
        )
        for trade in trades:
            address = _normalize_address(trade.get("proxyWallet") or trade.get("user") or trade.get("wallet"))
            if not address:
                continue
            outcome_index = trade.get("outcomeIndex")
            if outcome_index is None:
                continue
            try:
                outcome_index = int(outcome_index)
            except (TypeError, ValueError):
                continue
            side = (trade.get("side") or "").upper()
            size = safe_float(trade.get("size"))
            price = safe_float(trade.get("price"))
            notional = price * size
            ts = trade.get("timestamp") or 0
            try:
                ts_int = int(ts)
            except (TypeError, ValueError):
                ts_int = 0
            bucket = buckets[(address, outcome_index)]
            bucket["trade_count"] += 1
            bucket["last_ts"] = max(int(bucket["last_ts"]), ts_int)
            if side == "BUY":
                bucket["buys"] += size
                bucket["buys_notional"] += notional
            elif side == "SELL":
                bucket["sells"] += size
                bucket["sells_notional"] += notional

        winning = market.winning_outcome_index
        for (address, outcome_index), bucket in buckets.items():
            agg = aggregates.setdefault(address, WalletAggregate(address=address))
            agg.trades_count += int(bucket["trade_count"])
            ts = int(bucket["last_ts"])
            if ts > 0:
                agg.last_trade_at = max(agg.last_trade_at or 0, ts)
            net_size = bucket["buys"] - bucket["sells"]
            notional = bucket["buys_notional"]
            if net_size <= 0:
                agg.unresolved_markets.add(market.market_id)
                continue
            agg.notional_total += notional
            agg.notional_per_market.append(notional)
            category = market.category or "Uncategorised"
            if outcome_index == winning:
                agg.winning_markets.add(market.market_id)
                agg.category_results[category]["wins"] += 1
            else:
                agg.losing_markets.add(market.market_id)
                agg.category_results[category]["losses"] += 1

    # ---------------- internals: scoring ----------------

    def _score_wallets(
        self,
        aggregates: dict[str, WalletAggregate],
        usable_markets: list[ResolvedMarket],
    ) -> list[WalletDiscoveryRow]:
        rows: list[WalletDiscoveryRow] = []
        now_ts = int(datetime.now(UTC).timestamp())
        for address, agg in aggregates.items():
            wins = len(agg.winning_markets)
            losses = len(agg.losing_markets)
            sample = wins + losses
            unresolved = len(agg.unresolved_markets)
            confidence = compute_win_rate_confidence(sample)
            win_rate = round(wins / sample, 4) if sample else None
            avg_position = (sum(agg.notional_per_market) / len(agg.notional_per_market)) if agg.notional_per_market else 0.0
            med_position = float(median(agg.notional_per_market)) if agg.notional_per_market else 0.0
            best_category, category_payload = self._best_category(agg)

            recent_activity_score = self._recent_activity_score(agg.last_trade_at, now_ts)
            copyability_score = self._copyability_score(avg_position)

            warnings: list[str] = []
            reasons: list[str] = []

            score, warnings, reasons = self._compute_market_first_score(
                win_rate=win_rate,
                confidence=confidence,
                sample=sample,
                unresolved=unresolved,
                avg_position=avg_position,
                recent_activity_score=recent_activity_score,
                copyability_score=copyability_score,
                category_payload=category_payload,
                warnings=warnings,
                reasons=reasons,
            )
            tier = self._classify_tier(score, confidence, sample)
            composite = self._compute_composite(score, recent_activity_score, copyability_score)
            status = self._derive_status(tier, recent_activity_score, sample, confidence)

            rows.append(
                WalletDiscoveryRow(
                    address=address,
                    market_first_score=round(score, 2),
                    composite_score=round(composite, 2),
                    tier=tier,
                    status=status,
                    resolved_market_win_rate=win_rate,
                    win_rate_confidence=confidence,
                    resolved_markets_traded=sample,
                    resolved_winning_markets=wins,
                    resolved_losing_markets=losses,
                    unresolved_markets_count=unresolved,
                    best_category=best_category,
                    category_win_rates=category_payload,
                    recent_activity_score=round(recent_activity_score, 2),
                    copyability_score=round(copyability_score, 2),
                    total_resolved_notional=round(agg.notional_total, 2),
                    average_position_size=round(avg_position, 2),
                    median_position_size=round(med_position, 2),
                    warnings=warnings,
                    reasons=reasons,
                    data_source="polymarket_gamma+data",
                )
            )
        return rows

    def _best_category(self, agg: WalletAggregate) -> tuple[str | None, dict[str, dict[str, Any]]]:
        payload: dict[str, dict[str, Any]] = {}
        best_name: str | None = None
        best_rate = -1.0
        for category, stats in agg.category_results.items():
            wins = stats["wins"]
            losses = stats["losses"]
            sample = wins + losses
            if sample <= 0:
                continue
            rate = wins / sample
            payload[category] = {
                "wins": wins,
                "losses": losses,
                "sample": sample,
                "win_rate": round(rate, 4),
            }
            if sample >= 5 and rate > best_rate:
                best_name = category
                best_rate = rate
        return best_name, payload

    def _recent_activity_score(self, last_trade_at: int | None, now_ts: int) -> float:
        if not last_trade_at:
            return 0.0
        delta_seconds = max(0, now_ts - last_trade_at)
        delta_days = delta_seconds / 86400
        if delta_days <= 1:
            return 100.0
        if delta_days <= 7:
            return 90.0
        if delta_days <= 30:
            return 70.0
        if delta_days <= 90:
            return 45.0
        if delta_days <= 180:
            return 25.0
        return 10.0

    def _copyability_score(self, avg_position: float) -> float:
        if avg_position <= 0:
            return 30.0
        if avg_position <= 5_000:
            return 90.0
        if avg_position <= 25_000:
            return 75.0
        if avg_position <= 100_000:
            return 55.0
        if avg_position <= 500_000:
            return 35.0
        return 15.0

    def _compute_market_first_score(
        self,
        *,
        win_rate: float | None,
        confidence: str,
        sample: int,
        unresolved: int,
        avg_position: float,
        recent_activity_score: float,
        copyability_score: float,
        category_payload: dict[str, dict[str, Any]],
        warnings: list[str],
        reasons: list[str],
    ) -> tuple[float, list[str], list[str]]:
        # Sub-scores follow the v0.5 spec weights.
        # The win-rate portion is gated by confidence — INSUFFICIENT_DATA gives 0.
        win_rate_component = 0.0
        if win_rate is not None and confidence != "INSUFFICIENT_DATA":
            # MEDIUM gives ~85% of the budget so a sample of 30-99 resolved markets can still
            # produce a STRONG; LOW is heavily discounted because the rate is statistically thin.
            multiplier = 0.4 if confidence == "LOW" else 0.85 if confidence == "MEDIUM" else 1.0
            win_rate_component = win_rate * 25.0 * multiplier
        sample_component = min(20.0, sample / 100 * 20.0)
        category_component = self._category_specialization_component(category_payload)
        consistency_component = self._consistency_component(category_payload, win_rate)
        recent_component = recent_activity_score * 0.10  # max 10
        liquidity_component = copyability_score * 0.10  # max 10 (using copyability as proxy for tradable size)

        # Penalties.
        penalty = 0.0
        if sample < self.settings.market_first_min_sample_for_watch:
            penalty += 5.0
            warnings.append("low_resolved_sample")
        if sample > 0 and unresolved > 4 * sample:
            penalty += 3.0
            warnings.append("mostly_unresolved_positions")
        if avg_position > 500_000:
            penalty += 5.0
            warnings.append("position_size_too_large_to_copy")
        if win_rate is not None and win_rate >= 0.9 and sample < 20:
            penalty += 5.0
            warnings.append("suspicious_high_win_rate_small_sample")
        if recent_activity_score < 25:
            warnings.append("inactive_more_than_180_days")
        if win_rate is not None and confidence != "INSUFFICIENT_DATA":
            reasons.append(f"win_rate_{int(win_rate * 100)}pct_{confidence.lower()}_confidence")

        raw = (
            win_rate_component
            + sample_component
            + category_component
            + consistency_component
            + recent_component
            + liquidity_component
        )
        score = max(0.0, min(100.0, raw - penalty))
        return score, warnings, reasons

    def _category_specialization_component(self, category_payload: dict[str, dict[str, Any]]) -> float:
        if not category_payload:
            return 0.0
        # Reward best category having decent win rate AND a non-trivial sample.
        best_score = 0.0
        for stats in category_payload.values():
            sample = stats["sample"]
            rate = stats["win_rate"]
            if sample < 5:
                continue
            best_score = max(best_score, rate * min(1.0, sample / 30))
        return min(15.0, best_score * 15.0)

    def _consistency_component(self, category_payload: dict[str, dict[str, Any]], overall_rate: float | None) -> float:
        if not category_payload or overall_rate is None:
            return 0.0
        # Lower the consistency points the more wildly category rates differ.
        rates = [stats["win_rate"] for stats in category_payload.values() if stats["sample"] >= 5]
        if not rates:
            return 5.0
        deviation = sum(abs(r - overall_rate) for r in rates) / len(rates)
        consistency = max(0.0, 1.0 - min(1.0, deviation / 0.30))
        return consistency * 15.0

    def _classify_tier(self, score: float, confidence: str, sample: int) -> str:
        if sample < self.settings.market_first_min_sample_for_watch:
            return "INSUFFICIENT_DATA"
        if confidence == "INSUFFICIENT_DATA":
            return "INSUFFICIENT_DATA"
        # v0.5.2 strict ELITE rule: HIGH confidence only, which by construction
        # of WinRateEngine.compute_win_rate_confidence requires sample >= 100.
        # No combination of MEDIUM-confidence wallets can land in ELITE — even
        # a perfect 100% win rate at sample 99 stays in STRONG.
        if score >= 85 and confidence == "HIGH" and sample >= self.settings.market_first_min_sample_for_strong:
            return "ELITE"
        if score >= 75 and confidence in {"MEDIUM", "HIGH"} and sample >= self.settings.market_first_min_sample_for_strong:
            return "STRONG"
        if score >= 60:
            return "WATCH"
        if score >= 40:
            return "WEAK"
        return "IGNORE"

    def _compute_composite(self, market_first: float, recent_activity: float, copyability: float) -> float:
        # 70% historical / 20% current activity / 10% copyability — clamp to 0-100.
        return max(0.0, min(100.0, 0.70 * market_first + 0.20 * recent_activity + 0.10 * copyability))

    def _derive_status(self, tier: str, recent_activity: float, sample: int, confidence: str) -> str:
        if tier in {"ELITE", "STRONG"}:
            return "STRONG_AND_ACTIVE" if recent_activity >= 70 else "HISTORICAL_ELITE_INACTIVE"
        if tier == "WATCH":
            return "WATCH_ONLY"
        if tier == "WEAK":
            return "RECENTLY_ACTIVE_UNPROVEN" if recent_activity >= 70 else "IGNORE"
        if tier == "INSUFFICIENT_DATA":
            return "RECENTLY_ACTIVE_UNPROVEN" if recent_activity >= 70 else "IGNORE"
        return "IGNORE"

    # ---------------- internals: persistence + exports ----------------

    def _persist(
        self,
        rows: list[WalletDiscoveryRow],
        usable_markets: list[ResolvedMarket],
        rejected_markets: list[ResolvedMarket],
        market_trade_counts: dict[str, int],
    ) -> None:
        for row in rows:
            record = MarketFirstWalletRecord(
                address=row.address,
                market_first_score=row.market_first_score,
                composite_score=row.composite_score,
                tier=row.tier,
                status=row.status,
                resolved_market_win_rate=row.resolved_market_win_rate,
                win_rate_confidence=row.win_rate_confidence,
                resolved_markets_traded=row.resolved_markets_traded,
                resolved_winning_markets=row.resolved_winning_markets,
                resolved_losing_markets=row.resolved_losing_markets,
                best_category=row.best_category,
                category_win_rates=json.dumps(row.category_win_rates) if row.category_win_rates else None,
                recent_activity_score=row.recent_activity_score,
                copyability_score=row.copyability_score,
                total_resolved_notional=row.total_resolved_notional,
                average_position_size=row.average_position_size,
                median_position_size=row.median_position_size,
                warnings=json.dumps(row.warnings) if row.warnings else None,
                reasons=json.dumps(row.reasons) if row.reasons else None,
                data_source=row.data_source,
                audit_at=datetime.now(UTC),
            )
            self.session.merge(record)

        for market in usable_markets:
            self._persist_market(market, usable=True, trades_count=market_trade_counts.get(market.market_id, 0))
        for market in rejected_markets:
            self._persist_market(market, usable=False, trades_count=0)
        self.session.commit()

    def _persist_market(self, market: ResolvedMarket, usable: bool, trades_count: int) -> None:
        record = ResolvedMarketRecord(
            market_id=market.market_id or (market.condition_id or "unknown"),
            condition_id=market.condition_id,
            question=market.question,
            category=market.category,
            end_date=market.end_date,
            closed=market.closed,
            winning_outcome_index=market.winning_outcome_index,
            winning_outcome_name=market.winning_outcome_name,
            holders_count=0,
            trades_count=trades_count,
            usable=usable,
            rejection_reason=None if usable else (market.rejection_reason() or "LOW_DATA_QUALITY"),
            audited_at=datetime.now(UTC),
        )
        self.session.merge(record)

    def _write_exports(
        self,
        rows: list[WalletDiscoveryRow],
        usable_markets: list[ResolvedMarket],
        rejected_markets: list[ResolvedMarket],
        market_trade_counts: dict[str, int],
    ) -> None:
        with self.wallets_csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=WALLETS_CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for rank, row in enumerate(rows, start=1):
                payload = row.to_dict()
                payload["rank"] = rank
                payload["category_win_rates"] = json.dumps(payload["category_win_rates"]) if payload["category_win_rates"] else ""
                payload["warnings"] = ";".join(payload["warnings"])
                payload["reasons"] = ";".join(payload["reasons"])
                writer.writerow(payload)

        with self.markets_csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MARKETS_CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for market in usable_markets:
                writer.writerow(
                    {
                        "market_id": market.market_id,
                        "condition_id": market.condition_id,
                        "question": (market.question or "")[:200],
                        "category": market.category,
                        "end_date": market.end_date,
                        "closed": market.closed,
                        "winning_outcome": market.winning_outcome_name,
                        "holders_count": 0,
                        "trades_count": market_trade_counts.get(market.market_id, 0),
                        "usable": True,
                        "rejection_reason": "",
                    }
                )
            for market in rejected_markets:
                writer.writerow(
                    {
                        "market_id": market.market_id,
                        "condition_id": market.condition_id,
                        "question": (market.question or "")[:200],
                        "category": market.category,
                        "end_date": market.end_date,
                        "closed": market.closed,
                        "winning_outcome": market.winning_outcome_name,
                        "holders_count": 0,
                        "trades_count": 0,
                        "usable": False,
                        "rejection_reason": market.rejection_reason() or "LOW_DATA_QUALITY",
                    }
                )

        with self.rejected_csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=REJECTED_CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for market in rejected_markets:
                writer.writerow(
                    {
                        "market_id": market.market_id,
                        "condition_id": market.condition_id,
                        "question": (market.question or "")[:200],
                        "category": market.category,
                        "end_date": market.end_date,
                        "rejection_reason": market.rejection_reason() or "LOW_DATA_QUALITY",
                    }
                )

    def _write_diagnostics(
        self,
        usable_markets: list[ResolvedMarket],
        trades_by_market: dict[str, list[dict[str, Any]]],
        rows: list[WalletDiscoveryRow],
    ) -> None:
        """Sidecar diagnostics — answers the v0.5.5 questions:

        - How many trades / unique wallets does each market expose? Are we
          hitting the API ``limit`` for big markets?
        - What is the sample-size distribution across all wallets we saw?
        - How quickly do we discover *new* wallets as we walk the market list?
          (saturation curve — useful to decide whether bumping ``max_markets``
          further is worth it.)
        """
        per_market_trade_count: list[int] = []
        per_market_unique_wallets: list[int] = []
        cumulative_unique_addresses: set[str] = set()
        growth_points: list[dict[str, int]] = []
        sorted_markets = list(usable_markets)
        for idx, market in enumerate(sorted_markets, start=1):
            trades = trades_by_market.get(market.market_id, [])
            wallets_in_market = {
                _normalize_address(t.get("proxyWallet") or t.get("user") or t.get("wallet"))
                for t in trades
                if t.get("proxyWallet") or t.get("user") or t.get("wallet")
            }
            wallets_in_market.discard("")
            per_market_trade_count.append(len(trades))
            per_market_unique_wallets.append(len(wallets_in_market))
            cumulative_unique_addresses.update(wallets_in_market)
            # Snapshot every 250 markets so we can plot a saturation curve.
            if idx == 1 or idx % 250 == 0 or idx == len(sorted_markets):
                growth_points.append({"markets_scanned": idx, "cumulative_unique_wallets": len(cumulative_unique_addresses)})

        sample_sizes = [r.resolved_markets_traded for r in rows]
        bucket_edges = [1, 2, 5, 10, 30, 50, 100, 300, 1000, 5_000]
        sample_histogram: dict[str, int] = {}
        for edge_idx in range(len(bucket_edges)):
            lo = bucket_edges[edge_idx]
            hi = bucket_edges[edge_idx + 1] if edge_idx + 1 < len(bucket_edges) else None
            label = f">={lo}" if hi is None else f"{lo}..{hi - 1}"
            count = sum(1 for s in sample_sizes if s >= lo and (hi is None or s < hi))
            sample_histogram[label] = count

        # Trade-count limit detection: if a market hit exactly the requested
        # cap, we likely missed trades. Report fraction at-cap.
        trade_limit = self.settings.market_first_trades_per_market_limit
        at_cap = sum(1 for n in per_market_trade_count if n >= trade_limit)

        def _percentile(values: list[int], pct: float) -> int:
            if not values:
                return 0
            sv = sorted(values)
            k = max(0, min(len(sv) - 1, int(round(pct * (len(sv) - 1)))))
            return sv[k]

        diagnostics = {
            "markets_usable": len(usable_markets),
            "trades_per_market_limit": trade_limit,
            "markets_at_trade_limit": at_cap,
            "trade_count_per_market": {
                "min": min(per_market_trade_count) if per_market_trade_count else 0,
                "median": _percentile(per_market_trade_count, 0.50),
                "p95": _percentile(per_market_trade_count, 0.95),
                "max": max(per_market_trade_count) if per_market_trade_count else 0,
            },
            "unique_wallets_per_market": {
                "min": min(per_market_unique_wallets) if per_market_unique_wallets else 0,
                "median": _percentile(per_market_unique_wallets, 0.50),
                "p95": _percentile(per_market_unique_wallets, 0.95),
                "max": max(per_market_unique_wallets) if per_market_unique_wallets else 0,
            },
            "wallets_discovered_total": len(cumulative_unique_addresses),
            "growth_curve": growth_points,
            "sample_size_histogram": sample_histogram,
        }
        self.diagnostics_path.write_text(json.dumps(diagnostics, indent=2, default=str), encoding="utf-8")

    def _record_to_dict(self, record: MarketFirstWalletRecord) -> dict[str, Any]:
        category_payload: dict[str, Any] = {}
        try:
            category_payload = json.loads(record.category_win_rates) if record.category_win_rates else {}
        except Exception:
            category_payload = {}
        try:
            warnings = json.loads(record.warnings) if record.warnings else []
        except Exception:
            warnings = []
        try:
            reasons = json.loads(record.reasons) if record.reasons else []
        except Exception:
            reasons = []
        return {
            "address": record.address,
            "market_first_score": record.market_first_score,
            "composite_score": record.composite_score,
            "tier": record.tier,
            "status": record.status,
            "resolved_market_win_rate": record.resolved_market_win_rate,
            "win_rate_confidence": record.win_rate_confidence,
            "resolved_markets_traded": record.resolved_markets_traded,
            "resolved_winning_markets": record.resolved_winning_markets,
            "resolved_losing_markets": record.resolved_losing_markets,
            "best_category": record.best_category,
            "category_win_rates": category_payload,
            "recent_activity_score": record.recent_activity_score,
            "copyability_score": record.copyability_score,
            "total_resolved_notional": record.total_resolved_notional,
            "average_position_size": record.average_position_size,
            "median_position_size": record.median_position_size,
            "warnings": warnings,
            "reasons": reasons,
            "data_source": record.data_source,
            "audit_at": record.audit_at.isoformat() if record.audit_at else None,
        }

    def _market_record_to_dict(self, record: ResolvedMarketRecord) -> dict[str, Any]:
        return {
            "market_id": record.market_id,
            "condition_id": record.condition_id,
            "question": record.question,
            "category": record.category,
            "end_date": record.end_date,
            "closed": record.closed,
            "winning_outcome_index": record.winning_outcome_index,
            "winning_outcome_name": record.winning_outcome_name,
            "holders_count": record.holders_count,
            "trades_count": record.trades_count,
            "usable": record.usable,
            "rejection_reason": record.rejection_reason,
            "audited_at": record.audited_at.isoformat() if record.audited_at else None,
        }

    # ---------------- internals: warnings + conclusion ----------------

    def _compose_warnings(
        self,
        *,
        usable_markets: list[ResolvedMarket],
        rows: list[WalletDiscoveryRow],
        api_errors: int,
    ) -> list[str]:
        warnings: list[str] = []
        if not usable_markets:
            warnings.append("no_usable_resolved_markets_in_window")
        if api_errors > 0:
            warnings.append(f"api_errors_during_run={api_errors}")
        if rows and all(r.win_rate_confidence == "INSUFFICIENT_DATA" for r in rows):
            warnings.append("all_wallets_insufficient_sample_size")
        if rows:
            elite_or_strong = sum(1 for r in rows if r.tier in {"ELITE", "STRONG"})
            if elite_or_strong == 0:
                warnings.append("zero_elite_or_strong_wallets")
        return warnings

    def _derive_conclusion(
        self,
        *,
        usable_markets: list[ResolvedMarket],
        rows: list[WalletDiscoveryRow],
        api_errors: int,
        tier_breakdown: Counter,
        wallets_with_confidence: int,
    ) -> tuple[MarketFirstConclusion, str]:
        if not usable_markets:
            return (
                "MARKET_FIRST_INSUFFICIENT_DATA",
                "No usable resolved markets in the configured window. Try a larger MARKET_FIRST_DAYS_BACK.",
            )
        if api_errors >= max(5, len(usable_markets) // 4):
            return (
                "MARKET_FIRST_API_LIMITED",
                "Public Polymarket APIs returned errors for a sizeable fraction of markets. Slow down the cadence or retry later.",
            )
        if not rows:
            return (
                "MARKET_FIRST_INSUFFICIENT_DATA",
                "Markets were scanned but no wallets ended up net long the winning outcome — broaden the window.",
            )
        elite = tier_breakdown.get("ELITE", 0)
        strong = tier_breakdown.get("STRONG", 0)
        watch = tier_breakdown.get("WATCH", 0)
        if elite == 0 and strong == 0 and watch == 0:
            if wallets_with_confidence == 0:
                return (
                    "MARKET_FIRST_NEEDS_MORE_MARKETS",
                    "Every wallet has INSUFFICIENT_DATA. Increase MARKET_FIRST_DAYS_BACK or MARKET_FIRST_MAX_MARKETS_PER_RUN.",
                )
            return (
                "MARKET_FIRST_NO_ELITE_FOUND",
                "Wallets were ranked but none reached the ELITE/STRONG/WATCH bar — keep observing.",
            )
        if elite + strong >= 5:
            return (
                "MARKET_FIRST_GOOD",
                f"Found {elite + strong} ELITE/STRONG wallets across {len(usable_markets)} resolved markets. Discovery is producing usable signal.",
            )
        return (
            "MARKET_FIRST_DISCOVERY_READY",
            f"Discovery surfaced {elite + strong} ELITE/STRONG and {watch} WATCH wallets — usable as a starting set, room to grow.",
        )
