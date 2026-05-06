from app.providers.market_data.base import MarketDataProvider
from app.services.polymarket.gamma_client import GammaClient


class PolymarketGammaProvider(MarketDataProvider):
    def __init__(self) -> None:
        self.client = GammaClient()

    def fetch_active_markets(self) -> list[dict]:
        return self.client.fetch_active_markets()

    def fetch_market_details(self, market_id: str) -> dict:
        return self.client.fetch_market_details(market_id)
