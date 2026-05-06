"""Compute strict, resolved-market-based win rates for a wallet.

Rules (from v0.4.2 spec):
- A market only counts as won/lost when it is **resolved**, the winning outcome is
  known, and the wallet's net position on that outcome can be determined from its
  trade history.
- Trade-level win rate is reported only as an **indicator** alongside.
- We never invent a win or loss — unresolved or ambiguous trades go into
  ``unresolved_markets`` instead of being inferred.
- Confidence buckets follow the spec:
    0-9 resolved markets    -> INSUFFICIENT_DATA
    10-29                   -> LOW
    30-99                   -> MEDIUM
    100+                    -> HIGH

Two key services are reused:
- ``DataClient.fetch_wallet_trades(address)`` for the wallet's trade history.
- ``GammaClient.fetch_market_by_condition(condition_id)`` (or fetch_market_details)
  for resolution status and winning outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from sqlmodel import Session

from app.config import get_settings
from app.services.polymarket.data_client import DataClient
from app.services.polymarket.gamma_client import GammaClient
from app.services.polymarket.normalizers import parse_jsonish_list, safe_float
from app.services.smart_wallet_auditor import SmartWalletAuditor, _normalize_address

logger = logging.getLogger(__name__)


WinRateConfidence = Literal["INSUFFICIENT_DATA", "LOW", "MEDIUM", "HIGH"]


@dataclass
class MarketResolution:
    market_id: str
    resolved: bool
    winning_outcome_index: int | None
    winning_outcome_name: str | None
    outcomes: list[str] = field(default_factory=list)
    outcome_prices: list[float] = field(default_factory=list)
    data_source: str = "polymarket_gamma"
    notes: list[str] = field(default_factory=list)


@dataclass
class WalletMarketPosition:
    market_id: str
    outcome: str | None
    outcome_index: int | None
    net_size: float
    total_buys: float
    total_sells: float
    average_buy_price: float
    average_sell_price: float
    trade_count: int


@dataclass
class WinRateBreakdown:
    address: str
    resolved_market_win_rate: float | None
    trade_level_win_rate: float | None
    market_sample_size: int
    resolved_winning_markets: int
    resolved_losing_markets: int
    unresolved_markets_count: int
    win_rate_confidence: WinRateConfidence
    win_rate_warnings: list[str]
    data_source: str
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "resolved_market_win_rate": self.resolved_market_win_rate,
            "trade_level_win_rate": self.trade_level_win_rate,
            "market_sample_size": self.market_sample_size,
            "resolved_winning_markets": self.resolved_winning_markets,
            "resolved_losing_markets": self.resolved_losing_markets,
            "unresolved_markets_count": self.unresolved_markets_count,
            "win_rate_confidence": self.win_rate_confidence,
            "win_rate_warnings": list(self.win_rate_warnings),
            "data_source": self.data_source,
            "explanation": self.explanation,
        }


def compute_win_rate_confidence(market_sample_size: int) -> WinRateConfidence:
    if market_sample_size < 10:
        return "INSUFFICIENT_DATA"
    if market_sample_size < 30:
        return "LOW"
    if market_sample_size < 100:
        return "MEDIUM"
    return "HIGH"


class WinRateEngine:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        # Lazy clients so unit tests can monkey-patch them.
        self._data_client: DataClient | None = None
        self._gamma_client: GammaClient | None = None
        # In-memory resolution cache so a batch of wallets sharing markets only fetches once.
        self._resolution_cache: dict[str, MarketResolution] = {}

    # ---------------- public API ----------------

    def compute_wallet_win_rate(
        self,
        address: str,
        trades: list[dict[str, Any]] | None = None,
    ) -> WinRateBreakdown:
        normalized = _normalize_address(address)
        if trades is None:
            trades = self._fetch_trades(normalized)
        positions = self.group_wallet_trades_by_market(trades)
        resolved_winning = 0
        resolved_losing = 0
        unresolved = 0
        trade_wins = 0
        trade_losses = 0
        for position in positions:
            resolution = self.fetch_market_resolution(position.market_id)
            if not resolution.resolved or resolution.winning_outcome_index is None:
                unresolved += 1
                continue
            inferred_index = self._infer_position_outcome_index(position, resolution)
            if inferred_index is None:
                unresolved += 1
                continue
            if inferred_index == resolution.winning_outcome_index:
                resolved_winning += 1
                trade_wins += position.trade_count
            else:
                resolved_losing += 1
                trade_losses += position.trade_count

        market_sample_size = resolved_winning + resolved_losing
        confidence = compute_win_rate_confidence(market_sample_size)
        if market_sample_size > 0:
            resolved_market_win_rate: float | None = round(resolved_winning / market_sample_size, 4)
        else:
            resolved_market_win_rate = None
        trade_total = trade_wins + trade_losses
        trade_level_win_rate = round(trade_wins / trade_total, 4) if trade_total else None

        warnings = self.add_win_rate_warnings(
            resolved_market_win_rate=resolved_market_win_rate,
            trade_level_win_rate=trade_level_win_rate,
            confidence=confidence,
            unresolved=unresolved,
            resolved_winning=resolved_winning,
            resolved_losing=resolved_losing,
        )
        explanation = self.explain_win_rate(
            normalized,
            resolved_market_win_rate,
            confidence,
            market_sample_size,
            unresolved,
        )
        data_source = "polymarket_data+gamma" if trades and any(t.get("data_source") != "mock" for t in trades) else "mock"
        return WinRateBreakdown(
            address=normalized,
            resolved_market_win_rate=resolved_market_win_rate,
            trade_level_win_rate=trade_level_win_rate,
            market_sample_size=market_sample_size,
            resolved_winning_markets=resolved_winning,
            resolved_losing_markets=resolved_losing,
            unresolved_markets_count=unresolved,
            win_rate_confidence=confidence,
            win_rate_warnings=warnings,
            data_source=data_source,
            explanation=explanation,
        )

    def group_wallet_trades_by_market(
        self,
        trades: list[dict[str, Any]],
    ) -> list[WalletMarketPosition]:
        """Group raw wallet trades into one row per (market, outcome) bucket.

        For win-rate purposes we only keep positions where the wallet was net long
        (net_size > 0) — those are positions the wallet actually held into resolution.
        Net short / scalp / fully-exited positions go into ``unresolved`` counters
        because we cannot tell from the public API whether they were profitable
        without the realized-PnL endpoint (which Polymarket does not expose for free).
        """
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for trade in trades:
            market_id = str(trade.get("market_id") or trade.get("conditionId") or "").strip()
            if not market_id:
                continue
            outcome = trade.get("outcome") or trade.get("outcomeName") or ""
            outcome_key = str(outcome).strip().lower() or "_unknown_"
            bucket_key = (market_id, outcome_key)
            bucket = buckets.setdefault(
                bucket_key,
                {
                    "market_id": market_id,
                    "outcome": outcome or None,
                    "outcome_index": trade.get("outcome_index"),
                    "buys_size": 0.0,
                    "buys_notional": 0.0,
                    "sells_size": 0.0,
                    "sells_notional": 0.0,
                    "trade_count": 0,
                },
            )
            side = (trade.get("side") or "").upper()
            size = safe_float(trade.get("size"))
            price = safe_float(trade.get("price"))
            notional = safe_float(trade.get("notional_usd")) or (size * price)
            bucket["trade_count"] += 1
            if bucket["outcome_index"] is None and trade.get("outcome_index") is not None:
                bucket["outcome_index"] = trade.get("outcome_index")
            if side == "BUY":
                bucket["buys_size"] += size
                bucket["buys_notional"] += notional
            elif side == "SELL":
                bucket["sells_size"] += size
                bucket["sells_notional"] += notional
        positions: list[WalletMarketPosition] = []
        for entry in buckets.values():
            net_size = entry["buys_size"] - entry["sells_size"]
            avg_buy = entry["buys_notional"] / entry["buys_size"] if entry["buys_size"] else 0.0
            avg_sell = entry["sells_notional"] / entry["sells_size"] if entry["sells_size"] else 0.0
            positions.append(
                WalletMarketPosition(
                    market_id=entry["market_id"],
                    outcome=entry["outcome"],
                    outcome_index=entry["outcome_index"],
                    net_size=net_size,
                    total_buys=entry["buys_size"],
                    total_sells=entry["sells_size"],
                    average_buy_price=avg_buy,
                    average_sell_price=avg_sell,
                    trade_count=entry["trade_count"],
                )
            )
        return positions

    def fetch_market_resolution(self, market_id: str) -> MarketResolution:
        if not market_id:
            return MarketResolution(market_id="", resolved=False, winning_outcome_index=None, winning_outcome_name=None, notes=["empty_market_id"])
        if market_id in self._resolution_cache:
            return self._resolution_cache[market_id]
        if not self.settings.polymarket_public_enabled:
            resolution = self._mock_resolution(market_id)
            self._resolution_cache[market_id] = resolution
            return resolution
        try:
            payload: dict[str, Any] = {}
            looks_like_condition = market_id.startswith("0x") and len(market_id) >= 40
            if looks_like_condition:
                payload = self._gamma().fetch_market_by_condition(market_id)
            else:
                payload = self._gamma().fetch_market_details(market_id)
                if not payload:
                    payload = self._gamma().fetch_market_by_condition(market_id)
            if not payload:
                resolution = MarketResolution(
                    market_id=market_id,
                    resolved=False,
                    winning_outcome_index=None,
                    winning_outcome_name=None,
                    data_source="polymarket_gamma",
                    notes=["market_not_found"],
                )
            else:
                resolution = self._normalize_resolution(market_id, payload)
        except Exception as exc:
            logger.warning("Resolution fetch failed for %s: %s", market_id, exc)
            resolution = MarketResolution(
                market_id=market_id,
                resolved=False,
                winning_outcome_index=None,
                winning_outcome_name=None,
                data_source="unavailable",
                notes=[f"fetch_failed: {exc}"],
            )
        self._resolution_cache[market_id] = resolution
        return resolution

    def add_win_rate_warnings(
        self,
        *,
        resolved_market_win_rate: float | None,
        trade_level_win_rate: float | None,
        confidence: WinRateConfidence,
        unresolved: int,
        resolved_winning: int,
        resolved_losing: int,
    ) -> list[str]:
        warnings: list[str] = []
        if confidence == "INSUFFICIENT_DATA":
            warnings.append("insufficient_resolved_market_sample")
        if unresolved > (resolved_winning + resolved_losing):
            warnings.append("more_unresolved_than_resolved")
        if resolved_market_win_rate is not None and resolved_market_win_rate >= 0.85 and (resolved_winning + resolved_losing) < 30:
            warnings.append("high_win_rate_small_sample")
        if (
            resolved_market_win_rate is not None
            and trade_level_win_rate is not None
            and abs(resolved_market_win_rate - trade_level_win_rate) > 0.20
        ):
            warnings.append("market_vs_trade_winrate_divergence")
        return warnings

    def explain_win_rate(
        self,
        address: str,
        win_rate: float | None,
        confidence: WinRateConfidence,
        sample_size: int,
        unresolved: int,
    ) -> str:
        if win_rate is None:
            return f"{address}: no resolved markets in sample (unresolved={unresolved}); win rate is INSUFFICIENT_DATA."
        return (
            f"{address}: {win_rate * 100:.1f}% win rate over {sample_size} resolved markets "
            f"(confidence={confidence}, unresolved={unresolved})."
        )

    # ---------------- internals ----------------

    def _data(self) -> DataClient:
        if self._data_client is None:
            self._data_client = DataClient()
        return self._data_client

    def _gamma(self) -> GammaClient:
        if self._gamma_client is None:
            self._gamma_client = GammaClient()
        return self._gamma_client

    def _fetch_trades(self, address: str) -> list[dict[str, Any]]:
        # Reuse SmartWalletAuditor's logic so we honour mock vs real / no-silent-fallback rules.
        auditor = SmartWalletAuditor(self.session)
        return auditor.fetch_wallet_recent_trades(address)

    def _normalize_resolution(self, market_id: str, payload: dict[str, Any]) -> MarketResolution:
        outcomes = [str(item) for item in parse_jsonish_list(payload.get("outcomes"))]
        prices = [safe_float(item) for item in parse_jsonish_list(payload.get("outcomePrices"))]
        closed = bool(payload.get("closed"))
        active = payload.get("active")
        winning_index: int | None = None
        for idx, price in enumerate(prices):
            if price >= 0.999:
                winning_index = idx
                break
        if winning_index is None and closed and prices and len(set(prices)) > 1:
            winning_index = max(range(len(prices)), key=lambda i: prices[i])
        notes: list[str] = []
        if not closed:
            notes.append("market_not_closed")
        if winning_index is None:
            notes.append("winning_outcome_unknown")
        return MarketResolution(
            market_id=market_id,
            resolved=closed and winning_index is not None,
            winning_outcome_index=winning_index,
            winning_outcome_name=outcomes[winning_index] if winning_index is not None and winning_index < len(outcomes) else None,
            outcomes=outcomes,
            outcome_prices=prices,
            data_source="polymarket_gamma",
            notes=notes,
        )

    def _mock_resolution(self, market_id: str) -> MarketResolution:
        # In mock mode resolutions are deterministic so the win-rate path is exercised end-to-end:
        # markets ending in even digit hash → YES wins, odd → NO wins.
        try:
            seed = int.from_bytes(market_id.encode("utf-8")[:4] or b"\x00", "big")
        except Exception:
            seed = 0
        winning_index = seed % 2
        return MarketResolution(
            market_id=market_id,
            resolved=True,
            winning_outcome_index=winning_index,
            winning_outcome_name="YES" if winning_index == 0 else "NO",
            outcomes=["YES", "NO"],
            outcome_prices=[1.0 if winning_index == 0 else 0.0, 1.0 if winning_index == 1 else 0.0],
            data_source="mock",
        )

    def _infer_position_outcome_index(
        self,
        position: WalletMarketPosition,
        resolution: MarketResolution,
    ) -> int | None:
        # We only attribute a position to an outcome if the wallet was net long on it.
        if position.net_size <= 0:
            return None
        if position.outcome_index is not None:
            try:
                return int(position.outcome_index)
            except (TypeError, ValueError):
                pass
        if position.outcome and resolution.outcomes:
            outcome_lower = str(position.outcome).strip().lower()
            for idx, name in enumerate(resolution.outcomes):
                if str(name).strip().lower() == outcome_lower:
                    return idx
        return None

    # ---------------- batch helpers ----------------

    def compute_resolved_market_win_rate(self, addresses: Iterable[str]) -> dict[str, WinRateBreakdown]:
        return {addr: self.compute_wallet_win_rate(addr) for addr in addresses if addr}

    def compute_trade_level_win_rate(self, addresses: Iterable[str]) -> dict[str, float | None]:
        return {addr: self.compute_wallet_win_rate(addr).trade_level_win_rate for addr in addresses if addr}
