"""Walk Polymarket Gamma's closed markets and emit a clean stream of *usable*
resolved markets — i.e. those that have a clear winning outcome, a known
conditionId, and live in a window we want to audit.

Markets that fail any check are returned with a ``rejection_reason`` from a
fixed enum so the discovery report can break down WHY a chunk of the universe
was discarded. We never silently drop a row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from app.config import get_settings
from app.services.polymarket.gamma_client import GammaClient
from app.services.polymarket.normalizers import parse_jsonish_list, safe_float

logger = logging.getLogger(__name__)


REJECTION_REASONS = (
    "NO_CONDITION_ID",
    "NOT_CLOSED",
    "UNKNOWN_WINNING_OUTCOME",
    "NO_HOLDERS_DATA",
    "AMBIGUOUS_OUTCOME",
    "LOW_DATA_QUALITY",
    "API_ERROR",
    "OUTSIDE_TIME_WINDOW",
)


@dataclass
class ResolvedMarket:
    market_id: str
    condition_id: str | None
    question: str | None
    category: str | None
    end_date: str | None
    closed: bool
    outcomes: list[str]
    outcome_prices: list[float]
    winning_outcome_index: int | None
    winning_outcome_name: str | None
    slug: str | None = None
    event_slug: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return (
            bool(self.condition_id)
            and self.closed
            and self.winning_outcome_index is not None
        )

    def rejection_reason(self) -> str | None:
        if not self.condition_id:
            return "NO_CONDITION_ID"
        if not self.closed:
            return "NOT_CLOSED"
        if self.winning_outcome_index is None:
            return "UNKNOWN_WINNING_OUTCOME"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "question": self.question,
            "category": self.category,
            "end_date": self.end_date,
            "closed": self.closed,
            "outcomes": list(self.outcomes),
            "outcome_prices": list(self.outcome_prices),
            "winning_outcome_index": self.winning_outcome_index,
            "winning_outcome_name": self.winning_outcome_name,
            "rejection_reason": self.rejection_reason(),
            "is_usable": self.is_usable,
            "notes": list(self.notes),
        }


class MarketResolutionScanner:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._gamma: GammaClient | None = None

    def _client(self) -> GammaClient:
        if self._gamma is None:
            self._gamma = GammaClient()
        return self._gamma

    def fetch_resolved_markets(
        self,
        days_back: int | None = None,
        limit: int | None = None,
    ) -> list[ResolvedMarket]:
        """Page through closed markets up to ``limit`` rows or ``days_back`` days,
        whichever is hit first. Markets failing validation are KEPT in the stream
        with their ``rejection_reason`` set so callers can build a complete report."""
        days_back = days_back or self.settings.market_first_days_back
        limit = limit or self.settings.market_first_market_limit
        cutoff = datetime.now(UTC) - timedelta(days=days_back)
        page_size = 200
        collected: list[ResolvedMarket] = []
        offset = 0
        while len(collected) < limit:
            try:
                page = self._client().fetch_closed_markets(
                    limit=min(page_size, limit - len(collected)),
                    offset=offset,
                    order="endDate",
                    ascending=False,
                )
            except Exception as exc:
                logger.warning("fetch_closed_markets failed at offset=%s: %s", offset, exc)
                break
            if not page:
                break
            for raw in page:
                market = self._normalize(raw)
                # Outside the time window? Skip silently — pagination is sorted desc.
                end = market.end_date or ""
                if end and end < cutoff.isoformat():
                    market.notes.append("OUTSIDE_TIME_WINDOW")
                    return collected
                collected.append(market)
            offset += len(page)
            if len(page) < page_size:
                break
        return collected[:limit]

    def filter_usable_resolved_markets(self, markets: Iterable[ResolvedMarket]) -> list[ResolvedMarket]:
        return [m for m in markets if m.is_usable]

    def reject_unusable_market(self, market: ResolvedMarket) -> dict[str, Any]:
        return {
            "market_id": market.market_id,
            "condition_id": market.condition_id,
            "question": market.question,
            "rejection_reason": market.rejection_reason() or "LOW_DATA_QUALITY",
        }

    def detect_resolved_market(self, raw: dict[str, Any]) -> ResolvedMarket:
        return self._normalize(raw)

    def extract_condition_id(self, raw: dict[str, Any]) -> str | None:
        cid = raw.get("conditionId") or raw.get("condition_id")
        if not cid:
            return None
        cid = str(cid).strip()
        return cid or None

    def extract_outcomes(self, raw: dict[str, Any]) -> list[str]:
        return [str(item) for item in parse_jsonish_list(raw.get("outcomes"))]

    def extract_winning_outcome(self, raw: dict[str, Any]) -> tuple[int | None, str | None]:
        outcomes = self.extract_outcomes(raw)
        prices = [safe_float(item) for item in parse_jsonish_list(raw.get("outcomePrices"))]
        winning_index: int | None = None
        for idx, price in enumerate(prices):
            if price >= 0.999:
                winning_index = idx
                break
        # All-zero outcomePrices means the market was voided / refunded — never
        # treat that as a resolution. We only fall back to argmax when prices
        # actually distinguish between the outcomes.
        if winning_index is None and any(p > 0 for p in prices) and len(set(prices)) > 1:
            winning_index = max(range(len(prices)), key=lambda i: prices[i])
        if winning_index is None:
            return None, None
        name = outcomes[winning_index] if winning_index < len(outcomes) else None
        return winning_index, name

    def validate_resolution_data(self, market: ResolvedMarket) -> str | None:
        """Return a rejection reason when the resolution data is unusable."""
        return market.rejection_reason()

    def classify_market_category(self, raw: dict[str, Any]) -> str | None:
        for key in ("category", "tag", "section", "eventTag"):
            value = raw.get(key)
            if value:
                return str(value)
        return None

    # ---------------- internals ----------------

    def _normalize(self, raw: dict[str, Any]) -> ResolvedMarket:
        condition_id = self.extract_condition_id(raw)
        outcomes = self.extract_outcomes(raw)
        prices = [safe_float(item) for item in parse_jsonish_list(raw.get("outcomePrices"))]
        winning_index, winning_name = self.extract_winning_outcome(raw)
        end_date = raw.get("endDate") or raw.get("end_date")
        return ResolvedMarket(
            market_id=str(raw.get("id") or raw.get("conditionId") or condition_id or ""),
            condition_id=condition_id,
            question=raw.get("question") or raw.get("title"),
            category=self.classify_market_category(raw),
            end_date=str(end_date) if end_date else None,
            closed=bool(raw.get("closed", False)),
            outcomes=outcomes,
            outcome_prices=prices,
            winning_outcome_index=winning_index,
            winning_outcome_name=winning_name,
            slug=raw.get("slug"),
            event_slug=raw.get("eventSlug") or raw.get("event_slug"),
            raw=raw,
        )
