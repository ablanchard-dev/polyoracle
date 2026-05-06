from abc import ABC, abstractmethod
from typing import Any


class WalletDataProvider(ABC):
    @abstractmethod
    def fetch_leaderboard(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def fetch_wallet_profile(self, address: str) -> dict[str, Any]:
        raise NotImplementedError
