from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

EdgeClassification = Literal["STRONG_EDGE", "WEAK_EDGE", "NO_EDGE", "NEGATIVE_EDGE", "INSUFFICIENT_DATA"]


@dataclass
class EdgeBreakdown:
    raw_edge: float
    spread_impact: float
    slippage_impact: float
    delay_penalty: float
    late_entry_penalty: float
    price_deterioration: float
    copyable_edge: float
    score: float
    classification: EdgeClassification

    def to_dict(self) -> dict:
        return {
            "raw_edge": self.raw_edge,
            "spread_impact": self.spread_impact,
            "slippage_impact": self.slippage_impact,
            "delay_penalty": self.delay_penalty,
            "late_entry_penalty": self.late_entry_penalty,
            "price_deterioration": self.price_deterioration,
            "copyable_edge": self.copyable_edge,
            "score": self.score,
            "classification": self.classification,
        }


class CopyableEdgeEngine:
    def __init__(self) -> None:
        pass

    def compute_price_deterioration(self, original_price: float, current_price: float, side: str = "BUY") -> float:
        if original_price <= 0 or current_price <= 0:
            return 0.0
        if side.upper() == "BUY":
            return round(max(0.0, current_price - original_price) / original_price, 6)
        return round(max(0.0, original_price - current_price) / original_price, 6)

    def compute_copy_delay(self, traded_at: datetime | None, now: datetime | None = None) -> float:
        if traded_at is None:
            return 0.0
        now = now or datetime.now(UTC)
        if traded_at.tzinfo is None:
            traded_at = traded_at.replace(tzinfo=UTC)
        return round(max(0.0, (now - traded_at).total_seconds()), 2)

    def compute_spread_impact(self, spread_pct: float | None) -> float:
        if spread_pct is None:
            return 0.0
        return round(max(0.0, spread_pct / 2), 6)

    def compute_slippage_impact(self, slippage: float) -> float:
        return round(max(0.0, slippage), 6)

    def compute_liquidity_adjusted_size(self, requested_size_usd: float, total_depth_usd: float, max_pct: float = 0.05) -> float:
        if total_depth_usd <= 0:
            return 0.0
        cap = total_depth_usd * max_pct
        return round(min(requested_size_usd, cap), 2)

    def compute_late_entry_risk(self, copy_delay_seconds: float, price_deterioration: float) -> float:
        delay_factor = min(1.0, copy_delay_seconds / 1_800)
        deterioration_factor = min(1.0, price_deterioration / 0.05)
        return round((delay_factor * 0.6 + deterioration_factor * 0.4), 4)

    def compute_copyable_edge(
        self,
        wallet_score: float,
        signal_score: float,
        spread_pct: float | None,
        slippage: float,
        copy_delay_seconds: float,
        original_price: float,
        current_price: float,
        side: str = "BUY",
    ) -> EdgeBreakdown:
        raw_edge = max(0.0, ((signal_score + wallet_score) / 200) - 0.5)
        spread_impact = self.compute_spread_impact(spread_pct)
        slippage_impact = self.compute_slippage_impact(slippage)
        delay_penalty = min(0.05, copy_delay_seconds / 60_000)
        deterioration = self.compute_price_deterioration(original_price, current_price, side)
        late_entry_penalty = self.compute_late_entry_risk(copy_delay_seconds, deterioration) * 0.05
        copyable_edge = max(0.0, raw_edge - spread_impact - slippage_impact - delay_penalty - late_entry_penalty - deterioration)
        score = round(min(100.0, copyable_edge * 200 + wallet_score * 0.4 + signal_score * 0.4), 2)
        classification = self.classify_edge(copyable_edge)
        return EdgeBreakdown(
            raw_edge=round(raw_edge, 6),
            spread_impact=spread_impact,
            slippage_impact=slippage_impact,
            delay_penalty=round(delay_penalty, 6),
            late_entry_penalty=round(late_entry_penalty, 6),
            price_deterioration=deterioration,
            copyable_edge=round(copyable_edge, 6),
            score=score,
            classification=classification,
        )

    def classify_edge(self, copyable_edge: float) -> EdgeClassification:
        if copyable_edge <= 0:
            return "NEGATIVE_EDGE"
        if copyable_edge < 0.005:
            return "NO_EDGE"
        if copyable_edge < 0.02:
            return "WEAK_EDGE"
        return "STRONG_EDGE"
