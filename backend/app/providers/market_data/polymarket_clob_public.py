from app.services.polymarket.clob_client import ClobPublicClient


class PolymarketClobPublicProvider:
    def __init__(self) -> None:
        self.client = ClobPublicClient()

    def fetch_orderbook(self, token_id: str) -> dict:
        return self.client.fetch_orderbook(token_id)

    def fetch_midpoint(self, token_id: str) -> dict:
        return self.client.fetch_midpoint(token_id)
