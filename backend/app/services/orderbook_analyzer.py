from dataclasses import dataclass, field
from typing import Any, Literal

OrderbookQuality = Literal["EXCELLENT", "GOOD", "ACCEPTABLE", "BAD", "UNTRADABLE", "INSUFFICIENT_DATA"]


@dataclass
class OrderbookSummary:
    token_id: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    midpoint: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    total_depth: float = 0.0
    imbalance: float = 0.0
    quality: OrderbookQuality = "INSUFFICIENT_DATA"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "midpoint": self.midpoint,
            "spread": self.spread,
            "spread_pct": self.spread_pct,
            "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth,
            "total_depth": self.total_depth,
            "imbalance": self.imbalance,
            "quality": self.quality,
            "notes": list(self.notes),
        }


def _coerce_levels(raw: Any) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    if not raw:
        return levels
    iterable = raw if isinstance(raw, list) else []
    for item in iterable:
        try:
            if isinstance(item, dict):
                price = float(item.get("price") or item.get("p") or 0.0)
                size = float(item.get("size") or item.get("s") or item.get("amount") or 0.0)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price = float(item[0])
                size = float(item[1])
            else:
                continue
        except (TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            levels.append((price, size))
    return levels


class OrderbookAnalyzer:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}
        book = self.payload.get("orderbook") if isinstance(self.payload.get("orderbook"), dict) else self.payload
        self.bids: list[tuple[float, float]] = sorted(_coerce_levels(book.get("bids")), key=lambda x: x[0], reverse=True)
        self.asks: list[tuple[float, float]] = sorted(_coerce_levels(book.get("asks")), key=lambda x: x[0])
        self.token_id = str(self.payload.get("token_id") or book.get("token_id") or "") or None
        midpoint_raw = self.payload.get("midpoint")
        if isinstance(midpoint_raw, dict):
            try:
                self._midpoint_hint: float | None = float(midpoint_raw.get("mid") or midpoint_raw.get("midpoint") or 0.0) or None
            except (TypeError, ValueError):
                self._midpoint_hint = None
        else:
            self._midpoint_hint = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "OrderbookAnalyzer":
        return cls(payload)

    def compute_best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    def compute_best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    def compute_midpoint(self) -> float | None:
        bid = self.compute_best_bid()
        ask = self.compute_best_ask()
        if bid is not None and ask is not None:
            return round((bid + ask) / 2, 6)
        return self._midpoint_hint

    def compute_spread(self) -> float | None:
        bid = self.compute_best_bid()
        ask = self.compute_best_ask()
        if bid is None or ask is None:
            return None
        return round(max(0.0, ask - bid), 6)

    def compute_spread_pct(self) -> float | None:
        spread = self.compute_spread()
        midpoint = self.compute_midpoint()
        if spread is None or not midpoint:
            return None
        return round(spread / midpoint, 6)

    def compute_bid_depth(self) -> float:
        return round(sum(price * size for price, size in self.bids), 2)

    def compute_ask_depth(self) -> float:
        return round(sum(price * size for price, size in self.asks), 2)

    def compute_total_depth(self) -> float:
        return round(self.compute_bid_depth() + self.compute_ask_depth(), 2)

    def compute_imbalance(self) -> float:
        bid = self.compute_bid_depth()
        ask = self.compute_ask_depth()
        total = bid + ask
        if total <= 0:
            return 0.0
        return round((bid - ask) / total, 4)

    def estimate_slippage_for_size(self, size_usd: float) -> float:
        if size_usd <= 0:
            return 0.0
        levels = self.asks if self.asks else self.bids
        if not levels:
            return 0.05
        midpoint = self.compute_midpoint() or (levels[0][0] if levels else 0)
        if midpoint <= 0:
            return 0.05
        remaining = size_usd
        weighted_price = 0.0
        consumed = 0.0
        for price, size in levels:
            level_notional = price * size
            take = min(level_notional, remaining)
            weighted_price += take * price
            consumed += take
            remaining -= take
            if remaining <= 0:
                break
        if consumed <= 0:
            return 0.05
        average_fill = weighted_price / consumed
        slippage = abs(average_fill - midpoint) / midpoint if midpoint else 0.0
        if remaining > 0:
            slippage = max(slippage, 0.05)
        return round(min(0.50, slippage), 6)

    def estimate_fill_price(self, side: str, size_usd: float) -> float | None:
        levels = self.asks if side.upper() == "BUY" else self.bids
        if not levels:
            return None
        if side.upper() == "BUY":
            levels = sorted(levels, key=lambda x: x[0])
        else:
            levels = sorted(levels, key=lambda x: x[0], reverse=True)
        remaining = size_usd
        weighted_price = 0.0
        consumed = 0.0
        for price, size in levels:
            level_notional = price * size
            take = min(level_notional, remaining)
            weighted_price += take * price
            consumed += take
            remaining -= take
            if remaining <= 0:
                break
        if consumed <= 0:
            return None
        return round(weighted_price / consumed, 6)

    def classify_orderbook_quality(self) -> OrderbookQuality:
        spread_pct = self.compute_spread_pct()
        depth = self.compute_total_depth()
        if spread_pct is None and depth <= 0:
            return "INSUFFICIENT_DATA"
        if spread_pct is None or depth <= 0:
            return "BAD"
        if spread_pct <= 0.005 and depth >= 50_000:
            return "EXCELLENT"
        if spread_pct <= 0.015 and depth >= 10_000:
            return "GOOD"
        if spread_pct <= 0.03 and depth >= 2_000:
            return "ACCEPTABLE"
        if spread_pct <= 0.08 and depth >= 500:
            return "BAD"
        return "UNTRADABLE"

    def summarize(self) -> OrderbookSummary:
        summary = OrderbookSummary(
            token_id=self.token_id,
            best_bid=self.compute_best_bid(),
            best_ask=self.compute_best_ask(),
            midpoint=self.compute_midpoint(),
            spread=self.compute_spread(),
            spread_pct=self.compute_spread_pct(),
            bid_depth=self.compute_bid_depth(),
            ask_depth=self.compute_ask_depth(),
            total_depth=self.compute_total_depth(),
            imbalance=self.compute_imbalance(),
            quality=self.classify_orderbook_quality(),
        )
        if not self.bids:
            summary.notes.append("missing_bids")
        if not self.asks:
            summary.notes.append("missing_asks")
        if summary.spread_pct is not None and summary.spread_pct > 0.05:
            summary.notes.append("wide_spread")
        return summary
