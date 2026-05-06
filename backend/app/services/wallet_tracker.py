from sqlmodel import Session

from app.models.wallet import Wallet
from app.services.mock_data import mock_wallets


class WalletTracker:
    def __init__(self, session: Session) -> None:
        self.session = session

    def fetch_leaderboard(self) -> list[Wallet]:
        return sorted(mock_wallets(), key=lambda wallet: wallet.score, reverse=True)

    def fetch_wallet_profile(self, address: str) -> Wallet | None:
        return next((wallet for wallet in self.fetch_leaderboard() if wallet.address == address), None)

    def fetch_wallet_positions(self, address: str) -> list[dict[str, float | str]]:
        return [{"market_id": "mock-election-001", "outcome": "YES", "size": 8_500.0, "entry": 0.49}]

    def fetch_wallet_trades(self, address: str) -> list[dict[str, float | str]]:
        return [{"market_id": "mock-election-001", "outcome": "YES", "size": 2_000.0, "price": 0.54}]

    def compute_wallet_score(self, address: str) -> float:
        wallet = self.fetch_wallet_profile(address)
        if wallet is None:
            return 0.0
        sample_penalty = 10.0 if wallet.market_count < 30 else 0.0
        concentration_penalty = 5.0 if wallet.volume > 0 and wallet.market_count < 50 else 0.0
        raw = (
            25 * min(1, max(0, wallet.pnl / 150_000))
            + 20 * min(1, max(0, wallet.roi / 0.40))
            + 15 * min(1, wallet.win_rate)
            + 15 * min(1, wallet.market_count / 120)
            + 10 * 0.75
            + 10 * 0.8
            + 5 * 0.7
        )
        return round(max(0.0, raw - sample_penalty - concentration_penalty), 2)

    def detect_whales(self) -> list[Wallet]:
        return [wallet for wallet in self.fetch_leaderboard() if wallet.volume >= 750_000]

    def compute_whale_score(self, address: str) -> float:
        wallet = self.fetch_wallet_profile(address)
        if wallet is None:
            return 0.0
        return round(min(100.0, wallet.volume / 20_000), 2)

    def detect_suspicious_wallets(self) -> list[dict[str, str]]:
        return [
            {"address": wallet.address, "reason": "small sample with outsized volume"}
            for wallet in self.fetch_leaderboard()
            if wallet.market_count < 60 and wallet.volume > 400_000
        ]

    def blacklist_wallet(self, address: str) -> dict[str, str]:
        return {"address": address, "status": "blacklist"}

    def explain_wallet_score(self, address: str) -> dict[str, str | float]:
        wallet = self.fetch_wallet_profile(address)
        if wallet is None:
            return {"address": address, "score": 0.0, "explanation": "wallet not found"}
        return {
            "address": address,
            "score": self.compute_wallet_score(address),
            "explanation": "Score blends PnL, ROI, sample size, timing, specialization and risk penalties; win rate is not used alone.",
        }

    def classify_wallet_specialty(self, address: str) -> str:
        wallet = self.fetch_wallet_profile(address)
        return wallet.specialty if wallet and wallet.specialty else "Generalist"

    def update_watchlist(self, address: str, status: str = "watched") -> dict[str, str]:
        return {"address": address, "status": status}
