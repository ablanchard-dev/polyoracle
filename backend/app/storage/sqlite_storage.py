from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.models.market import Market
from app.models.signal import Signal
from app.models.trade import PaperTrade
from app.models.wallet import Wallet
from app.storage.base import StorageProvider
from app.storage.file_storage import FileStorage


class SQLiteStorage(StorageProvider):
    def __init__(self, session: Session, sqlite_path: str = "./data/polyoracle.db") -> None:
        self.session = session
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_storage = FileStorage(str(self.sqlite_path.parent))

    def save_market_snapshot(self, payload: dict[str, Any]) -> str:
        return self.file_storage.save_market_snapshot(payload)

    def save_wallet_snapshot(self, payload: dict[str, Any]) -> str:
        return self.file_storage.save_wallet_snapshot(payload)

    def save_signal(self, payload: dict[str, Any]) -> str:
        return self.file_storage.save_signal(payload)

    def save_trade(self, payload: dict[str, Any]) -> str:
        return self.file_storage.save_trade(payload)

    def get_markets(self) -> list[dict[str, Any]]:
        return [market.model_dump() for market in self.session.exec(select(Market)).all()]

    def get_wallets(self) -> list[dict[str, Any]]:
        return [wallet.model_dump() for wallet in self.session.exec(select(Wallet)).all()]

    def get_signals(self) -> list[dict[str, Any]]:
        return [signal.model_dump() for signal in self.session.exec(select(Signal)).all()]

    def get_trades(self) -> list[dict[str, Any]]:
        return [trade.model_dump() for trade in self.session.exec(select(PaperTrade)).all()]

    def export_trades(self, path: str | None = None) -> str:
        return self.file_storage.export_trades(path)

    def export_signals(self, path: str | None = None) -> str:
        return self.file_storage.export_signals(path)
