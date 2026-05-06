from app.providers.wallet_data.base import WalletDataProvider
from app.services.mock_data import mock_wallets


class MockWalletProvider(WalletDataProvider):
    def fetch_leaderboard(self) -> list[dict]:
        return [wallet.model_dump() for wallet in mock_wallets()]

    def fetch_wallet_profile(self, address: str) -> dict:
        return next((wallet for wallet in self.fetch_leaderboard() if wallet["address"] == address), {})
