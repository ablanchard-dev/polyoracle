from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from app.config import get_settings
from app.models.wallet import (
    Wallet,
    WalletAudit,
    WalletCategoryStat,
    WalletDailyStat,
    WalletScoreSnapshot,
    WalletTradeRecord,
    WalletWatchEntry,
)
from app.services.mock_data import mock_wallets
from app.services.polymarket.data_client import DataClient
from app.storage.file_storage import FileStorage

logger = logging.getLogger(__name__)


WALLET_TIERS = ["ELITE", "STRONG", "WATCH", "WEAK", "IGNORE", "SUSPICIOUS", "INSUFFICIENT_DATA"]


@dataclass
class WalletScoreBreakdown:
    pnl_score: float = 0.0
    roi_score: float = 0.0
    sample_size_score: float = 0.0
    consistency_score: float = 0.0
    copyability_score: float = 0.0
    timing_score: float = 0.0
    exit_quality_score: float = 0.0
    category_specialization_score: float = 0.0
    liquidity_quality_score: float = 0.0
    risk_management_score: float = 0.0
    recent_activity_score: float = 0.0
    confidence_score: float = 0.0
    win_rate_score: float = 0.0
    resolved_market_win_rate: float | None = None
    market_sample_size: int = 0
    win_rate_confidence: str = "INSUFFICIENT_DATA"
    win_rate_warning: str | None = None
    penalties: float = 0.0
    smart_score: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pnl_score": self.pnl_score,
            "roi_score": self.roi_score,
            "sample_size_score": self.sample_size_score,
            "consistency_score": self.consistency_score,
            "copyability_score": self.copyability_score,
            "timing_score": self.timing_score,
            "exit_quality_score": self.exit_quality_score,
            "category_specialization_score": self.category_specialization_score,
            "liquidity_quality_score": self.liquidity_quality_score,
            "risk_management_score": self.risk_management_score,
            "recent_activity_score": self.recent_activity_score,
            "confidence_score": self.confidence_score,
            "win_rate_score": self.win_rate_score,
            "resolved_market_win_rate": self.resolved_market_win_rate,
            "market_sample_size": self.market_sample_size,
            "win_rate_confidence": self.win_rate_confidence,
            "win_rate_warning": self.win_rate_warning,
            "penalties": self.penalties,
            "smart_score": self.smart_score,
            "notes": list(self.notes),
        }


@dataclass
class WalletAuditResult:
    address: str
    smart_score: float
    whale_score: float
    reliability: float
    copyability: float
    confidence: float
    sample_size: int
    tier: str
    specialty: str | None
    pnl: float
    roi: float
    win_rate: float
    average_position_size: float
    max_drawdown: float
    suspicious: bool
    suspicious_reason: str | None
    data_source: str
    breakdown: WalletScoreBreakdown
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "smart_score": self.smart_score,
            "whale_score": self.whale_score,
            "reliability": self.reliability,
            "copyability": self.copyability,
            "confidence": self.confidence,
            "sample_size": self.sample_size,
            "tier": self.tier,
            "specialty": self.specialty,
            "pnl": self.pnl,
            "roi": self.roi,
            "win_rate": self.win_rate,
            "average_position_size": self.average_position_size,
            "max_drawdown": self.max_drawdown,
            "suspicious": self.suspicious,
            "suspicious_reason": self.suspicious_reason,
            "data_source": self.data_source,
            "notes": list(self.notes),
            "breakdown": self.breakdown.to_dict(),
        }


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _normalize_address(value: Any) -> str:
    return str(value or "").strip().lower()


class SmartWalletAuditor:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.file_storage = FileStorage(str(self.settings.resolved_sqlite_path.parent))

    # ---------------- discovery ----------------

    def _data_client(self) -> DataClient:
        return DataClient()

    def discover_top_wallets(self, limit: int = 100) -> list[dict[str, Any]]:
        """Discover wallets that are actually trading right now.

        Primary source: `/trades` (recent global trade feed) aggregated by `proxyWallet`.
        The legacy `/leaderboard` endpoint returns 404 in production and is no longer
        used as a primary path.

        Fallback policy:
        - When `polymarket_public_enabled` is false → mock (development convenience).
        - When the public APIs fail and `mock_data_enabled` is true → mock fallback.
        - When the public APIs fail and `mock_data_enabled` is false → empty list with
          `data_source="unavailable"` (NEVER silent mock).
        """
        return self.discover_top_wallets_detailed(limit)["wallets"]

    def discover_top_wallets_detailed(self, limit: int = 100) -> dict[str, Any]:
        if not self.settings.polymarket_public_enabled:
            wallets = self._mock_top_wallets(limit)
            return {"wallets": wallets, "data_source": "mock", "errors": [], "ranking_basis": "mock"}

        errors: list[str] = []
        # Primary path: recent trades aggregation.
        try:
            wallets = self._discover_via_recent_trades(limit)
            if wallets:
                self.file_storage.save_wallet_snapshot(
                    {
                        "source": "polymarket_data_recent_trades",
                        "captured_at": datetime.now(UTC).isoformat(),
                        "count": len(wallets),
                        "wallets": wallets,
                    }
                )
                return {
                    "wallets": wallets,
                    "data_source": "polymarket_data_recent_trades",
                    "errors": errors,
                    "ranking_basis": "recent_trade_notional",
                }
            errors.append("recent_trades_returned_empty")
        except Exception as exc:
            logger.warning("Recent trades discovery failed: %s", exc)
            errors.append(f"recent_trades_failed: {exc}")

        # Legacy path (will 404 in production but kept for API symmetry).
        try:
            payload = self._data_client().fetch_leaderboard()
            wallets = self._normalize_leaderboard(payload)
            if wallets:
                wallets = wallets[:limit]
                return {
                    "wallets": wallets,
                    "data_source": "polymarket_data_leaderboard",
                    "errors": errors,
                    "ranking_basis": "leaderboard",
                }
            errors.append("leaderboard_returned_empty")
        except Exception as exc:
            errors.append(f"leaderboard_failed: {exc}")

        if self.settings.mock_data_enabled:
            return {
                "wallets": self._mock_top_wallets(limit),
                "data_source": "mock_fallback",
                "errors": errors,
                "ranking_basis": "mock_fallback",
            }
        return {"wallets": [], "data_source": "unavailable", "errors": errors, "ranking_basis": "unavailable"}

    def _discover_via_recent_trades(self, limit: int) -> list[dict[str, Any]]:
        """Aggregate the global recent trade feed by wallet, rank by notional traded."""
        client = self._data_client()
        # Fetch a generous window so we don't end up with only a handful of distinct wallets.
        feed_limit = max(limit * 20, 500)
        raw = client.fetch_recent_trades(limit=feed_limit) or []
        per_wallet: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            address = _normalize_address(entry.get("proxyWallet") or entry.get("user") or entry.get("wallet"))
            if not address:
                continue
            price = _safe_float(entry.get("price"))
            size = _safe_float(entry.get("size"))
            notional = price * size if price and size else 0.0
            ts = entry.get("timestamp")
            slot = per_wallet.setdefault(
                address,
                {
                    "address": address,
                    "trades": 0,
                    "notional": 0.0,
                    "buys": 0,
                    "sells": 0,
                    "markets": set(),
                    "last_trade_at": None,
                    "name": entry.get("name") or "",
                    "pseudonym": entry.get("pseudonym") or "",
                    "data_source": "polymarket_data_recent_trades",
                },
            )
            slot["trades"] += 1
            slot["notional"] += notional
            side = (entry.get("side") or "").upper()
            if side == "BUY":
                slot["buys"] += 1
            elif side == "SELL":
                slot["sells"] += 1
            condition_id = entry.get("conditionId")
            if condition_id:
                slot["markets"].add(condition_id)
            if ts is not None:
                try:
                    ts_int = int(ts)
                    if slot["last_trade_at"] is None or ts_int > slot["last_trade_at"]:
                        slot["last_trade_at"] = ts_int
                except (TypeError, ValueError):
                    pass
        ranked = sorted(per_wallet.values(), key=lambda item: item["notional"], reverse=True)[:limit]
        wallets: list[dict[str, Any]] = []
        for record in ranked:
            last_trade_iso: str | None = None
            if record["last_trade_at"]:
                try:
                    last_trade_iso = datetime.fromtimestamp(record["last_trade_at"], tz=UTC).isoformat()
                except (OSError, ValueError, OverflowError):
                    last_trade_iso = None
            wallets.append(
                {
                    "address": record["address"],
                    "pnl": 0.0,
                    "roi": 0.0,
                    "win_rate": 0.0,
                    "market_count": len(record["markets"]),
                    "volume": round(record["notional"], 2),
                    "trades_observed": record["trades"],
                    "buys": record["buys"],
                    "sells": record["sells"],
                    "specialty": None,
                    "name": record["name"],
                    "pseudonym": record["pseudonym"],
                    "last_trade_at": last_trade_iso,
                    "data_source": record["data_source"],
                }
            )
        return wallets

    def _mock_top_wallets(self, limit: int) -> list[dict[str, Any]]:
        wallets = mock_wallets()
        records: list[dict[str, Any]] = []
        for wallet in wallets[:limit]:
            records.append(
                {
                    "address": wallet.address,
                    "pnl": wallet.pnl,
                    "roi": wallet.roi,
                    "win_rate": wallet.win_rate,
                    "market_count": wallet.market_count,
                    "volume": wallet.volume,
                    "specialty": wallet.specialty,
                    "last_trade_at": wallet.last_trade_at.isoformat() if wallet.last_trade_at else None,
                    "data_source": "mock",
                }
            )
        return records

    def _normalize_leaderboard(self, payload: Any) -> list[dict[str, Any]]:
        items: list[Any] = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            for key in ("leaderboard", "results", "users", "data"):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
        normalized: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            address = _normalize_address(
                raw.get("address")
                or raw.get("user")
                or raw.get("wallet")
                or raw.get("proxyWallet")
                or raw.get("user_address")
            )
            if not address:
                continue
            normalized.append(
                {
                    "address": address,
                    "pnl": _safe_float(raw.get("pnl") or raw.get("profitLoss")),
                    "roi": _safe_float(raw.get("roi") or raw.get("returnOnInvestment")),
                    "win_rate": _safe_float(raw.get("winRate") or raw.get("win_rate")),
                    "market_count": _safe_int(raw.get("marketsTraded") or raw.get("market_count") or raw.get("markets")),
                    "volume": _safe_float(raw.get("volume") or raw.get("totalVolume") or raw.get("notional")),
                    "specialty": raw.get("specialty") or raw.get("category"),
                    "last_trade_at": raw.get("lastTradeAt") or raw.get("last_trade_at"),
                    "data_source": "polymarket_data",
                }
            )
        return normalized

    # ---------------- public profile / activity ----------------

    def fetch_wallet_public_profile(self, address: str) -> dict[str, Any]:
        if not self.settings.polymarket_public_enabled:
            return {"address": address, "data_source": "mock"}
        try:
            positions = self._data_client().fetch_wallet_positions(address)
            return {"address": address, "positions": positions, "data_source": "polymarket_data"}
        except Exception as exc:
            logger.warning("Public profile fetch failed for %s: %s", address, exc)
            return {"address": address, "data_source": "unavailable", "error": str(exc)}

    def fetch_wallet_recent_trades(self, address: str, limit: int | None = None) -> list[dict[str, Any]]:
        cap = limit or self.settings.max_wallet_trades_per_audit
        if not self.settings.polymarket_public_enabled:
            return self._mock_wallet_trades(address)[:cap]
        try:
            raw = self._data_client().fetch_wallet_trades(address)
            normalized = self._normalize_trades(raw, address)
            return normalized[:cap]
        except Exception as exc:
            logger.warning("Public trades fetch failed for %s: %s", address, exc)
            if self.settings.mock_data_enabled:
                return self._mock_wallet_trades(address)[:cap]
            return []

    def fetch_wallet_positions(self, address: str) -> list[dict[str, Any]]:
        profile = self.fetch_wallet_public_profile(address)
        positions = profile.get("positions") if isinstance(profile, dict) else None
        if isinstance(positions, list):
            return positions
        return []

    def fetch_wallet_markets(self, address: str) -> list[str]:
        trades = self.fetch_wallet_recent_trades(address)
        seen: dict[str, None] = {}
        for trade in trades:
            market_id = str(trade.get("market_id") or "")
            if market_id and market_id not in seen:
                seen[market_id] = None
        return list(seen.keys())

    def _normalize_trades(self, raw: Any, address: str) -> list[dict[str, Any]]:
        items: list[Any] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            for key in ("activity", "trades", "results", "data"):
                if isinstance(raw.get(key), list):
                    items = raw[key]
                    break
        normalized: list[dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            price = _safe_float(entry.get("price") or entry.get("avgPrice"))
            size = _safe_float(entry.get("size") or entry.get("amount") or entry.get("shares"))
            notional = _safe_float(entry.get("notional") or entry.get("usdSize") or entry.get("value"))
            if notional <= 0 and price > 0 and size > 0:
                notional = price * size
            normalized.append(
                {
                    "trade_id": str(entry.get("id") or entry.get("transactionHash") or uuid4()),
                    "address": address,
                    "market_id": str(
                        entry.get("market")
                        or entry.get("marketId")
                        or entry.get("conditionId")
                        or entry.get("conditionID")
                        or ""
                    ),
                    "outcome": entry.get("outcome") or entry.get("outcomeName"),
                    "outcome_index": entry.get("outcomeIndex"),
                    "side": (entry.get("side") or entry.get("action") or "").upper() or None,
                    "price": price,
                    "size": size,
                    "notional_usd": notional,
                    "traded_at": _parse_dt(entry.get("timestamp") or entry.get("tradedAt") or entry.get("time")),
                    "data_source": "polymarket_data",
                }
            )
        return normalized

    def _mock_wallet_trades(self, address: str) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        # Prices are anchored near the mock market's current YES price (0.54) so the freshest
        # entries can survive price-deterioration checks and demonstrate paper auto trading.
        # Later trades drift further away to exercise LATE_ENTRY / NO_COPYABLE_EDGE paths.
        offsets_seconds = [60, 600, 7_200, 21_600, 43_200, 64_800, 86_400, 129_600]
        prices = [0.538, 0.535, 0.530, 0.520, 0.510, 0.500, 0.490, 0.480]
        sizes = [1_500, 1_750, 2_000, 2_250, 2_500, 2_750, 3_000, 3_250]
        return [
            {
                "trade_id": f"mock-{address}-{idx}",
                "address": address,
                "market_id": "mock-election-001",
                "outcome": "YES",
                "side": "BUY",
                "price": prices[idx],
                "size": sizes[idx],
                "notional_usd": prices[idx] * sizes[idx],
                "traded_at": now - timedelta(seconds=offsets_seconds[idx]),
                "data_source": "mock",
            }
            for idx in range(len(offsets_seconds))
        ]

    # ---------------- scoring ----------------

    def compute_smart_wallet_score(
        self,
        profile: dict[str, Any],
        trades: list[dict[str, Any]] | None = None,
        win_rate_breakdown: Any | None = None,
    ) -> WalletScoreBreakdown:
        trades = trades or []
        sample_size = max(_safe_int(profile.get("market_count")), len(trades))
        breakdown = WalletScoreBreakdown()
        pnl = _safe_float(profile.get("pnl"))
        roi = _safe_float(profile.get("roi"))
        win_rate = _safe_float(profile.get("win_rate"))
        volume = _safe_float(profile.get("volume"))

        breakdown.pnl_score = round(min(20.0, max(0.0, pnl) / 150_000 * 20), 2)
        breakdown.roi_score = round(min(15.0, max(0.0, roi) / 0.40 * 15), 2)
        breakdown.sample_size_score = round(min(10.0, sample_size / 100 * 10), 2)
        breakdown.consistency_score = round(min(10.0, win_rate * 12), 2) if win_rate else 0.0
        breakdown.copyability_score = round(self.compute_wallet_copyability(profile, trades) * 0.10, 2)
        breakdown.timing_score = round(self._compute_timing_score(trades) * 0.10, 2)
        breakdown.exit_quality_score = round(self._compute_exit_quality_score(trades) * 0.10, 2)
        breakdown.category_specialization_score = round(self._compute_category_score(profile, trades) * 0.05, 2)
        breakdown.liquidity_quality_score = round(self._compute_liquidity_quality_score(trades) * 0.05, 2)
        breakdown.risk_management_score = round(self._compute_risk_management_score(profile, trades) * 0.05, 2)
        breakdown.recent_activity_score = round(self._compute_recent_activity_score(profile, trades) * 0.05, 2)
        breakdown.confidence_score = round(self._compute_confidence_score(sample_size, profile) * 0.05, 2)

        win_rate_score, win_rate_meta = self._compute_win_rate_score(win_rate_breakdown)
        breakdown.win_rate_score = win_rate_score
        breakdown.resolved_market_win_rate = win_rate_meta["rate"]
        breakdown.market_sample_size = win_rate_meta["sample"]
        breakdown.win_rate_confidence = win_rate_meta["confidence"]
        breakdown.win_rate_warning = win_rate_meta["warning"]

        penalties = 0.0
        if sample_size < 20:
            penalties += 10
            breakdown.notes.append("low_sample_size")
        if win_rate >= 0.75 and sample_size < 30:
            penalties += 8
            breakdown.notes.append("suspicious_win_rate_small_sample")
        if pnl > 50_000 and sample_size < 10:
            penalties += 10
            breakdown.notes.append("pnl_concentrated_in_few_trades")
        if volume > 0 and sample_size > 0 and volume / max(sample_size, 1) > 250_000:
            penalties += 5
            breakdown.notes.append("very_concentrated_positions")

        raw = (
            breakdown.pnl_score
            + breakdown.roi_score
            + breakdown.sample_size_score
            + breakdown.consistency_score
            + breakdown.copyability_score
            + breakdown.timing_score
            + breakdown.exit_quality_score
            + breakdown.category_specialization_score
            + breakdown.liquidity_quality_score
            + breakdown.risk_management_score
            + breakdown.recent_activity_score
            + breakdown.confidence_score
            + breakdown.win_rate_score
        )
        breakdown.penalties = round(penalties, 2)
        breakdown.smart_score = round(max(0.0, min(100.0, raw - penalties)), 2)
        return breakdown

    def _compute_win_rate_score(self, win_rate_breakdown: Any | None) -> tuple[float, dict[str, Any]]:
        """Win rate is capped at 10% of the total score and only contributes meaningfully
        when ``win_rate_confidence`` is at least MEDIUM. INSUFFICIENT_DATA returns 0
        so a small-sample 90% win rate cannot game the tier classification."""
        meta: dict[str, Any] = {"rate": None, "sample": 0, "confidence": "INSUFFICIENT_DATA", "warning": None}
        if win_rate_breakdown is None:
            return 0.0, meta
        rate = getattr(win_rate_breakdown, "resolved_market_win_rate", None)
        confidence = getattr(win_rate_breakdown, "win_rate_confidence", "INSUFFICIENT_DATA")
        sample = getattr(win_rate_breakdown, "market_sample_size", 0)
        warnings = getattr(win_rate_breakdown, "win_rate_warnings", []) or []
        meta["rate"] = rate
        meta["sample"] = sample
        meta["confidence"] = confidence
        meta["warning"] = warnings[0] if warnings else None
        if rate is None or confidence == "INSUFFICIENT_DATA":
            return 0.0, meta
        rate_clamped = max(0.0, min(1.0, rate))
        if confidence == "LOW":
            multiplier = 0.3
        elif confidence == "MEDIUM":
            multiplier = 0.7
        else:  # HIGH
            multiplier = 1.0
        score = rate_clamped * 10.0 * multiplier
        return round(score, 2), meta

    def compute_whale_score(self, profile: dict[str, Any]) -> float:
        volume = _safe_float(profile.get("volume"))
        return round(min(100.0, volume / 20_000), 2)

    def compute_wallet_reliability(self, profile: dict[str, Any], trades: list[dict[str, Any]] | None = None) -> float:
        trades = trades or []
        sample_size = max(_safe_int(profile.get("market_count")), len(trades))
        win_rate = _safe_float(profile.get("win_rate"))
        roi = _safe_float(profile.get("roi"))
        if sample_size < 20:
            return round(min(40.0, sample_size * 2), 2)
        consistency = win_rate * 60
        roi_quality = min(40.0, max(0.0, roi) / 0.30 * 40)
        return round(min(100.0, consistency + roi_quality), 2)

    def compute_wallet_copyability(self, profile: dict[str, Any], trades: list[dict[str, Any]] | None = None) -> float:
        trades = trades or []
        if not trades:
            volume = _safe_float(profile.get("volume"))
            sample_size = _safe_int(profile.get("market_count"))
            if sample_size <= 0:
                return 0.0
            avg_position = volume / sample_size if sample_size else 0.0
            if avg_position <= 0:
                return 30.0
            return round(min(80.0, max(20.0, 90.0 - min(70.0, avg_position / 10_000))), 2)
        avg_notional = sum(_safe_float(trade.get("notional_usd")) for trade in trades) / max(len(trades), 1)
        if avg_notional <= 0:
            return 30.0
        if avg_notional <= 5_000:
            return 90.0
        if avg_notional <= 25_000:
            return 75.0
        if avg_notional <= 100_000:
            return 55.0
        if avg_notional <= 500_000:
            return 35.0
        return 15.0

    def classify_wallet_specialty(self, address: str, trades: list[dict[str, Any]] | None = None) -> str:
        trades = trades if trades is not None else self.fetch_wallet_recent_trades(address)
        counts: dict[str, int] = {}
        for trade in trades:
            category = str(trade.get("category") or trade.get("market_category") or "Generalist")
            counts[category] = counts.get(category, 0) + 1
        if not counts:
            return "Generalist"
        top_category, _ = max(counts.items(), key=lambda item: item[1])
        return top_category

    def detect_suspicious_wallet(self, profile: dict[str, Any], trades: list[dict[str, Any]] | None = None) -> tuple[bool, str | None]:
        trades = trades or []
        sample_size = max(_safe_int(profile.get("market_count")), len(trades))
        win_rate = _safe_float(profile.get("win_rate"))
        pnl = _safe_float(profile.get("pnl"))
        volume = _safe_float(profile.get("volume"))
        if sample_size < 5 and pnl > 25_000:
            return True, "Outsized PnL with very small sample"
        if win_rate > 0.85 and sample_size < 15:
            return True, "Implausibly high win rate on tiny sample"
        if volume > 0 and sample_size > 0 and volume / max(sample_size, 1) > 750_000:
            return True, "Concentrated positions per market"
        return False, None

    # ---------------- audit pipeline ----------------

    def audit_wallet(
        self,
        address: str,
        profile: dict[str, Any] | None = None,
        win_rate_breakdown: Any | None = None,
    ) -> WalletAuditResult:
        profile = dict(profile or {})
        profile.setdefault("address", address)
        if not profile.get("pnl") and not profile.get("market_count"):
            for entry in self.discover_top_wallets(limit=200):
                if _normalize_address(entry.get("address")) == _normalize_address(address):
                    profile.update(entry)
                    break
        trades = self.fetch_wallet_recent_trades(address)
        breakdown = self.compute_smart_wallet_score(profile, trades, win_rate_breakdown=win_rate_breakdown)
        whale_score = self.compute_whale_score(profile)
        reliability = self.compute_wallet_reliability(profile, trades)
        copyability = self.compute_wallet_copyability(profile, trades)
        confidence = self._compute_confidence_score(max(_safe_int(profile.get("market_count")), len(trades)), profile)
        suspicious, suspicious_reason = self.detect_suspicious_wallet(profile, trades)
        sample_size = max(_safe_int(profile.get("market_count")), len(trades))
        average_position = (
            sum(_safe_float(trade.get("notional_usd")) for trade in trades) / max(len(trades), 1)
            if trades
            else _safe_float(profile.get("volume")) / max(sample_size or 1, 1)
        )
        max_drawdown = self._compute_max_drawdown(trades)
        tier = self._classify_tier(
            smart_score=breakdown.smart_score,
            sample_size=sample_size,
            suspicious=suspicious,
            confidence=confidence,
        )
        result = WalletAuditResult(
            address=address,
            smart_score=breakdown.smart_score,
            whale_score=whale_score,
            reliability=reliability,
            copyability=copyability,
            confidence=round(confidence, 2),
            sample_size=sample_size,
            tier=tier,
            specialty=self.classify_wallet_specialty(address, trades),
            pnl=_safe_float(profile.get("pnl")),
            roi=_safe_float(profile.get("roi")),
            win_rate=_safe_float(profile.get("win_rate")),
            average_position_size=round(average_position, 2),
            max_drawdown=max_drawdown,
            suspicious=suspicious,
            suspicious_reason=suspicious_reason,
            data_source=str(profile.get("data_source") or "unknown"),
            breakdown=breakdown,
            notes=list(breakdown.notes),
        )
        self._persist_audit(result, profile, trades)
        if self.settings.auto_watchlist_enabled:
            self.update_auto_watchlist(result)
        return result

    def audit_wallet_batch(self, addresses: list[str] | None = None, limit: int | None = None) -> list[WalletAuditResult]:
        if addresses is None:
            top = self.discover_top_wallets(limit=limit or self.settings.top_wallets_target)
            addresses = [entry.get("address") for entry in top if entry.get("address")]
        else:
            addresses = [addr for addr in addresses if addr]
        results: list[WalletAuditResult] = []
        # build a lookup for profile data
        cache: dict[str, dict[str, Any]] = {}
        for entry in self.discover_top_wallets(limit=max(len(addresses), 50)):
            addr = _normalize_address(entry.get("address"))
            if addr:
                cache[addr] = entry
        for address in addresses:
            try:
                profile = cache.get(_normalize_address(address))
                results.append(self.audit_wallet(address, profile))
            except Exception as exc:
                logger.warning("Wallet audit failed for %s: %s", address, exc)
        return results

    def update_auto_watchlist(self, audit: WalletAuditResult) -> None:
        if audit.tier in ("ELITE", "STRONG"):
            self._upsert_watch_entry(audit.address, "watch", reason=f"auto:{audit.tier}")
        elif audit.suspicious:
            self._upsert_watch_entry(audit.address, "blacklist", reason=audit.suspicious_reason or "auto:suspicious")

    def export_wallet_audit_report(self) -> dict[str, Any]:
        audits = list(self.session.exec(select(WalletAudit).order_by(WalletAudit.smart_score.desc())).all())
        if not audits:
            return {
                "generated_at": datetime.now(UTC).isoformat(),
                "wallets": [],
                "tier_breakdown": {tier: 0 for tier in WALLET_TIERS},
                "note": "No audited wallets yet. Run /wallets/audit/run-batch to populate.",
            }
        tier_breakdown: dict[str, int] = {tier: 0 for tier in WALLET_TIERS}
        wallets_payload: list[dict[str, Any]] = []
        for audit in audits:
            tier_breakdown[audit.tier] = tier_breakdown.get(audit.tier, 0) + 1
            wallets_payload.append(
                {
                    "address": audit.address,
                    "smart_score": audit.smart_score,
                    "whale_score": audit.whale_score,
                    "reliability": audit.reliability,
                    "copyability": audit.copyability,
                    "confidence": audit.confidence,
                    "tier": audit.tier,
                    "specialty": audit.specialty,
                    "pnl": audit.pnl,
                    "roi": audit.roi,
                    "win_rate": audit.win_rate,
                    "sample_size": audit.sample_size,
                    "suspicious": audit.suspicious,
                    "data_source": audit.data_source,
                    "audit_at": audit.audit_at.isoformat() if audit.audit_at else None,
                }
            )
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "wallets": wallets_payload,
            "tier_breakdown": tier_breakdown,
        }

    # ---------------- queries ----------------

    def list_audited_wallets(self, limit: int = 100) -> list[WalletAudit]:
        return list(self.session.exec(select(WalletAudit).order_by(WalletAudit.smart_score.desc()).limit(limit)).all())

    def list_watchlist(self) -> list[dict[str, Any]]:
        entries = list(self.session.exec(select(WalletWatchEntry)).all())
        return [
            {
                "address": entry.address,
                "list_type": entry.list_type,
                "reason": entry.reason,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries
        ]

    def stats(self) -> dict[str, Any]:
        audits = list(self.session.exec(select(WalletAudit)).all())
        tier_breakdown: dict[str, int] = {tier: 0 for tier in WALLET_TIERS}
        for audit in audits:
            tier_breakdown[audit.tier] = tier_breakdown.get(audit.tier, 0) + 1
        return {
            "audited_count": len(audits),
            "tier_breakdown": tier_breakdown,
            "watchlist_count": len(self.list_watchlist()),
        }

    def get_wallet(self, address: str) -> dict[str, Any] | None:
        record = self.session.get(Wallet, address)
        if record is None:
            return None
        return record.model_dump()

    def get_latest_audit(self, address: str) -> WalletAudit | None:
        statement = (
            select(WalletAudit)
            .where(WalletAudit.address == address)
            .order_by(WalletAudit.audit_at.desc())
            .limit(1)
        )
        return self.session.exec(statement).first()

    def get_wallet_trades(self, address: str, limit: int = 200) -> list[WalletTradeRecord]:
        statement = (
            select(WalletTradeRecord)
            .where(WalletTradeRecord.address == address)
            .order_by(WalletTradeRecord.traded_at.desc())
            .limit(limit)
        )
        return list(self.session.exec(statement).all())

    # ---------------- internals ----------------

    def _classify_tier(self, smart_score: float, sample_size: int, suspicious: bool, confidence: float) -> str:
        if suspicious:
            return "SUSPICIOUS"
        if sample_size < 10 or confidence < 30:
            return "INSUFFICIENT_DATA"
        if smart_score >= 80:
            return "ELITE"
        if smart_score >= 65:
            return "STRONG"
        if smart_score >= 50:
            return "WATCH"
        if smart_score >= 35:
            return "WEAK"
        return "IGNORE"

    def _compute_timing_score(self, trades: list[dict[str, Any]]) -> float:
        if not trades:
            return 40.0
        recent = [trade for trade in trades if (trade.get("traded_at") or datetime.now(UTC)) >= datetime.now(UTC) - timedelta(days=14)]
        ratio = len(recent) / max(len(trades), 1)
        return round(min(100.0, 40.0 + ratio * 60), 2)

    def _compute_exit_quality_score(self, trades: list[dict[str, Any]]) -> float:
        if not trades:
            return 50.0
        sells = [trade for trade in trades if (trade.get("side") or "").upper() == "SELL"]
        if not sells:
            return 40.0
        ratio = len(sells) / max(len(trades), 1)
        return round(min(100.0, 30.0 + ratio * 70), 2)

    def _compute_category_score(self, profile: dict[str, Any], trades: list[dict[str, Any]]) -> float:
        specialty = (profile.get("specialty") or "").strip()
        if specialty:
            return 80.0
        return 40.0 if trades else 20.0

    def _compute_liquidity_quality_score(self, trades: list[dict[str, Any]]) -> float:
        if not trades:
            return 50.0
        avg_notional = sum(_safe_float(trade.get("notional_usd")) for trade in trades) / max(len(trades), 1)
        if avg_notional <= 0:
            return 40.0
        if avg_notional <= 50_000:
            return 90.0
        if avg_notional <= 250_000:
            return 70.0
        if avg_notional <= 1_000_000:
            return 50.0
        return 30.0

    def _compute_risk_management_score(self, profile: dict[str, Any], trades: list[dict[str, Any]]) -> float:
        max_dd = self._compute_max_drawdown(trades)
        pnl = _safe_float(profile.get("pnl"))
        if pnl <= 0 and max_dd <= 0:
            return 30.0
        ratio = max_dd / max(pnl, 1)
        if ratio < 0.05:
            return 90.0
        if ratio < 0.15:
            return 70.0
        if ratio < 0.30:
            return 50.0
        return 30.0

    def _compute_recent_activity_score(self, profile: dict[str, Any], trades: list[dict[str, Any]]) -> float:
        last_at = _parse_dt(profile.get("last_trade_at"))
        if not last_at and trades:
            last_at = max((trade.get("traded_at") for trade in trades if trade.get("traded_at")), default=None)
        if not last_at:
            return 30.0
        delta_hours = (datetime.now(UTC) - last_at).total_seconds() / 3600
        if delta_hours <= 24:
            return 95.0
        if delta_hours <= 72:
            return 80.0
        if delta_hours <= 168:
            return 60.0
        if delta_hours <= 720:
            return 40.0
        return 20.0

    def _compute_confidence_score(self, sample_size: int, profile: dict[str, Any]) -> float:
        if sample_size <= 0:
            return 0.0
        if profile.get("data_source") == "mock":
            base = min(80.0, sample_size * 1.2)
        else:
            base = min(100.0, sample_size * 1.5)
        return round(base, 2)

    def _compute_max_drawdown(self, trades: list[dict[str, Any]]) -> float:
        if not trades:
            return 0.0
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for trade in trades:
            pnl = _safe_float(trade.get("realized_pnl") or trade.get("pnl"))
            equity += pnl
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return round(abs(max_dd), 2)

    def _persist_audit(self, audit: WalletAuditResult, profile: dict[str, Any], trades: list[dict[str, Any]]) -> None:
        wallet = self.session.get(Wallet, audit.address)
        if wallet is None:
            wallet = Wallet(address=audit.address)
        wallet.score = audit.smart_score
        wallet.pnl = audit.pnl
        wallet.roi = audit.roi
        wallet.win_rate = audit.win_rate
        wallet.market_count = audit.sample_size
        wallet.volume = _safe_float(profile.get("volume"))
        wallet.specialty = audit.specialty
        wallet.last_trade_at = _parse_dt(profile.get("last_trade_at")) or wallet.last_trade_at
        wallet.tier = audit.tier
        wallet.confidence = audit.confidence
        wallet.data_source = audit.data_source
        wallet.updated_at = datetime.now(UTC)
        if audit.suspicious:
            wallet.status = "blacklist"
        elif audit.tier in ("ELITE", "STRONG"):
            wallet.status = "watched"
        self.session.add(wallet)

        record = WalletAudit(
            id=str(uuid4()),
            address=audit.address,
            smart_score=audit.smart_score,
            whale_score=audit.whale_score,
            reliability=audit.reliability,
            copyability=audit.copyability,
            confidence=audit.confidence,
            sample_size=audit.sample_size,
            tier=audit.tier,
            specialty=audit.specialty,
            pnl=audit.pnl,
            roi=audit.roi,
            win_rate=audit.win_rate,
            average_position_size=audit.average_position_size,
            max_drawdown=audit.max_drawdown,
            suspicious=audit.suspicious,
            suspicious_reason=audit.suspicious_reason,
            data_source=audit.data_source,
            notes=json.dumps(audit.notes) if audit.notes else None,
        )
        self.session.add(record)

        snapshot = WalletScoreSnapshot(
            id=str(uuid4()),
            address=audit.address,
            pnl_score=audit.breakdown.pnl_score,
            roi_score=audit.breakdown.roi_score,
            sample_size_score=audit.breakdown.sample_size_score,
            consistency_score=audit.breakdown.consistency_score,
            copyability_score=audit.breakdown.copyability_score,
            timing_score=audit.breakdown.timing_score,
            exit_quality_score=audit.breakdown.exit_quality_score,
            category_specialization_score=audit.breakdown.category_specialization_score,
            liquidity_quality_score=audit.breakdown.liquidity_quality_score,
            risk_management_score=audit.breakdown.risk_management_score,
            recent_activity_score=audit.breakdown.recent_activity_score,
            confidence_score=audit.breakdown.confidence_score,
            penalty_total=audit.breakdown.penalties,
            smart_score=audit.breakdown.smart_score,
        )
        self.session.add(snapshot)

        for trade in trades[: self.settings.max_wallet_trades_per_audit]:
            trade_record = WalletTradeRecord(
                id=str(trade.get("trade_id") or uuid4()),
                address=audit.address,
                market_id=trade.get("market_id"),
                outcome=trade.get("outcome"),
                side=trade.get("side"),
                price=_safe_float(trade.get("price")),
                size=_safe_float(trade.get("size")),
                notional_usd=_safe_float(trade.get("notional_usd")),
                traded_at=trade.get("traded_at") if isinstance(trade.get("traded_at"), datetime) else _parse_dt(trade.get("traded_at")),
                data_source=str(trade.get("data_source") or audit.data_source),
            )
            self.session.merge(trade_record)

        for category, count in self._aggregate_categories(trades).items():
            stat = WalletCategoryStat(
                id=f"{audit.address}::{category}",
                address=audit.address,
                category=category,
                trades=count,
                win_rate=audit.win_rate,
                edge=audit.smart_score / 100,
            )
            self.session.merge(stat)

        for day, day_stat in self._aggregate_daily(trades).items():
            self.session.merge(
                WalletDailyStat(
                    id=f"{audit.address}::{day}",
                    address=audit.address,
                    day=day,
                    trades=day_stat["trades"],
                    notional_usd=day_stat["notional"],
                    realized_pnl=day_stat["pnl"],
                    win_rate=audit.win_rate,
                )
            )

        self.session.commit()

    def _aggregate_categories(self, trades: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trade in trades:
            category = str(trade.get("category") or trade.get("market_category") or "Generalist")
            counts[category] = counts.get(category, 0) + 1
        return counts

    def _aggregate_daily(self, trades: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
        rows: dict[str, dict[str, float]] = {}
        for trade in trades:
            traded_at = trade.get("traded_at") if isinstance(trade.get("traded_at"), datetime) else _parse_dt(trade.get("traded_at"))
            if not traded_at:
                continue
            day = traded_at.date().isoformat()
            row = rows.setdefault(day, {"trades": 0, "notional": 0.0, "pnl": 0.0})
            row["trades"] += 1
            row["notional"] += _safe_float(trade.get("notional_usd"))
            row["pnl"] += _safe_float(trade.get("realized_pnl"))
        return rows

    def _upsert_watch_entry(self, address: str, list_type: str, reason: str | None) -> None:
        entry = self.session.get(WalletWatchEntry, address)
        if entry is None:
            entry = WalletWatchEntry(address=address, list_type=list_type, reason=reason)
        else:
            entry.list_type = list_type
            entry.reason = reason
            entry.created_at = entry.created_at or datetime.now(UTC)
        self.session.merge(entry)
        self.session.commit()
