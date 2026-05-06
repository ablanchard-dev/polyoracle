from app.providers.wallet_data.base import WalletDataProvider
from app.services.polymarket.data_client import DataClient


class PolymarketDataProvider(WalletDataProvider):
    def __init__(self) -> None:
        self.client = DataClient()

    def fetch_leaderboard(self) -> list[dict]:
        return self.client.fetch_leaderboard()

    def fetch_wallet_profile(self, address: str) -> dict:
        return {"address": address, "positions": self.client.fetch_wallet_positions(address)}
