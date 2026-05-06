import json
import logging
from datetime import UTC, datetime

from sqlmodel import Session

from app.config import get_settings
from app.models.market import Market
from app.services.mock_data import mock_markets
from app.services.polymarket.clob_client import ClobPublicClient
from app.services.polymarket.gamma_client import GammaClient
from app.services.polymarket.normalizers import normalize_gamma_market
from app.storage.file_storage import FileStorage

logger = logging.getLogger(__name__)


class MarketScanner:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.file_storage = FileStorage(str(self.settings.resolved_sqlite_path.parent))

    def fetch_active_markets(self) -> list[Market]:
        if not self.settings.polymarket_public_enabled:
            markets = mock_markets()
            self.upsert_markets(markets)
            return markets

        try:
            raw_markets = GammaClient().fetch_active_markets(limit=self.settings.market_fetch_limit)
            markets = [normalize_gamma_market(payload) for payload in raw_markets if payload.get("id") or payload.get("conditionId")]
            if not markets:
                raise ValueError("Gamma API returned no usable markets")
            self.save_market_snapshot(markets, source="polymarket_gamma")
            self.upsert_markets(markets)
            return markets
        except Exception as exc:
            logger.warning("Falling back to mock markets after Gamma API failure: %s", exc)
            markets = mock_markets()
            if self.settings.mock_data_enabled:
                self.save_market_snapshot(markets, source="mock_fallback")
            self.upsert_markets(markets)
            return markets

    def fetch_market_details(self, market_id: str) -> Market | None:
        if self.settings.polymarket_public_enabled:
            # Hex IDs (0x...) are conditionIds: Gamma returns 422 on /markets/{id}
            # for those — route them through ?condition_id= instead.
            client = GammaClient()
            looks_like_condition = isinstance(market_id, str) and market_id.startswith("0x") and len(market_id) >= 40
            try:
                payload = (
                    client.fetch_market_by_condition(market_id)
                    if looks_like_condition
                    else client.fetch_market_details(market_id)
                )
                if payload:
                    market = normalize_gamma_market(payload)
                    self.upsert_markets([market])
                    self.file_storage.save_market_snapshot({"source": "polymarket_gamma_detail", "market": market.model_dump()})
                    return market
            except Exception as exc:
                logger.warning("Gamma market detail failed for %s: %s", market_id, exc)
        return next((market for market in self.fetch_active_markets() if market.id == market_id), None)

    def fetch_orderbook(self, token_id: str) -> dict:
        if not token_id:
            return {"token_id": token_id, "error": "missing token_id"}
        try:
            client = ClobPublicClient()
            orderbook = client.fetch_orderbook(token_id)
            midpoint = client.fetch_midpoint(token_id)
            payload = {
                "token_id": token_id,
                "orderbook": orderbook,
                "midpoint": midpoint,
                "data_source": "polymarket_clob_public",
                "captured_at": datetime.now(UTC).isoformat(),
            }
            if self.settings.orderbook_snapshot_enabled:
                self.file_storage._write_json("orderbooks", payload)
            return payload
        except Exception as exc:
            logger.warning("CLOB orderbook failed for %s: %s", token_id, exc)
            return {"token_id": token_id, "error": str(exc), "data_source": "clob_error"}

    def fetch_first_market_orderbook(self, market_id: str) -> dict:
        market = self.fetch_market_details(market_id)
        if market is None or not market.clob_token_ids:
            return {"market_id": market_id, "error": "market has no clob token ids"}
        token_ids = json.loads(market.clob_token_ids)
        token_id = str(token_ids[0]) if token_ids else ""
        return self.fetch_orderbook(token_id)

    def upsert_markets(self, markets: list[Market]) -> None:
        for market in markets:
            self.session.merge(market)
        self.session.commit()

    def save_market_snapshot(self, markets: list[Market], source: str) -> str:
        return self.file_storage.save_market_snapshot(
            {
                "source": source,
                "captured_at": datetime.now(UTC).isoformat(),
                "count": len(markets),
                "markets": [market.model_dump() for market in markets],
            }
        )

    def compute_market_liquidity_score(self, market: Market) -> float:
        """v0.7.2 B6 fix — log-scale 0-100 score calibrated for Polymarket
        market sizes ($100 floor, $10M ceiling).

        The legacy formula ``min(100, liquidity / 2500)`` caps at 100 only
        for liquidity ≥ $250k. Polymarket median is ~$50k → score 19/100,
        which CapitalAllocator's `min_liquidity_score=30` always rejected.
        With log-scale, $50k → ~54 (median quality) and the gate is
        meaningful.

        Mapping (Polymarket calibration):
          $0       → 0
          $100     → 0    (log10 = 2; (2-2)/5 = 0)
          $1 000   → 20
          $10 000  → 40
          $100 000 → 60
          $1 M     → 80
          $10 M    → 100  (log10 = 7; (7-2)/5 = 100)
          $50 k    → 53.6 (typical Polymarket median)
        """
        import math
        liquidity = float(market.liquidity or 0.0)
        if liquidity <= 100.0:
            return 0.0
        log_vol = math.log10(liquidity)
        score = (log_vol - 2.0) / (7.0 - 2.0) * 100.0
        return max(0.0, min(100.0, score))

    def compute_spread_score(self, market: Market) -> float:
        return max(0.0, 100.0 - (market.spread * 1_000))

    def detect_hot_markets(self) -> list[Market]:
        return [market for market in self.fetch_active_markets() if market.volume_24h > 50_000 and market.spread < 0.03]

    def compute_volume_score(self, market: Market) -> float:
        return min(100.0, market.volume_24h / 2_000)

    def compute_market_opportunity_score(self, market: Market) -> float:
        return round(
            0.35 * self.compute_volume_score(market)
            + 0.25 * self.compute_market_liquidity_score(market)
            + 0.25 * self.compute_spread_score(market)
            + 0.15 * min(100, market.open_interest / 10_000),
            2,
        )

    def reject_bad_markets(self) -> list[dict[str, str]]:
        rejected = []
        for market in self.fetch_active_markets():
            if market.liquidity < 1_000:
                rejected.append({"market_id": market.id, "reason": "liquidity too low"})
            elif market.spread > 0.05:
                rejected.append({"market_id": market.id, "reason": "spread too wide"})
        return rejected

    def rank_markets(self) -> list[Market]:
        markets = self.fetch_active_markets()
        for market in markets:
            market.opportunity_score = self.compute_market_opportunity_score(market)
        return sorted(markets, key=lambda item: item.opportunity_score, reverse=True)
