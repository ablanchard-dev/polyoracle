from abc import ABC, abstractmethod
from typing import Any


class MarketDataProvider(ABC):
    @abstractmethod
    def fetch_active_markets(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def fetch_market_details(self, market_id: str) -> dict[str, Any]:
        raise NotImplementedError
