from abc import ABC, abstractmethod
from typing import Any


class StorageProvider(ABC):
    @abstractmethod
    def save_market_snapshot(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    @abstractmethod
    def save_wallet_snapshot(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    @abstractmethod
    def save_signal(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    @abstractmethod
    def save_trade(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_markets(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_wallets(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_signals(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_trades(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def export_trades(self, path: str | None = None) -> str:
        raise NotImplementedError

    @abstractmethod
    def export_signals(self, path: str | None = None) -> str:
        raise NotImplementedError
