from app.providers.market_data.base import MarketDataProvider
from app.services.mock_data import mock_markets


class MockMarketDataProvider(MarketDataProvider):
    def fetch_active_markets(self) -> list[dict]:
        return [market.model_dump() for market in mock_markets()]

    def fetch_market_details(self, market_id: str) -> dict:
        return next((market for market in self.fetch_active_markets() if market["id"] == market_id), {})
