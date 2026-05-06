import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.base import StorageProvider


class FileStorage(StorageProvider):
    def __init__(self, root: str = "./data") -> None:
        self.root = Path(root)
        self.snapshot_dirs = {
            "markets": self.root / "snapshots" / "markets",
            "wallets": self.root / "snapshots" / "wallets",
            "signals": self.root / "snapshots" / "signals",
            "orderbooks": self.root / "snapshots" / "orderbooks",
        }
        self.exports_dir = self.root / "exports"
        self.logs_dir = self.root / "logs"
        self.ensure_layout()

    def ensure_layout(self) -> None:
        for path in [*self.snapshot_dirs.values(), self.exports_dir, self.logs_dir]:
            path.mkdir(parents=True, exist_ok=True)

    def _write_json(self, folder: str, payload: dict[str, Any]) -> str:
        target = self.snapshot_dirs[folder] / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.json"
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return str(target)

    def save_market_snapshot(self, payload: dict[str, Any]) -> str:
        return self._write_json("markets", payload)

    def save_wallet_snapshot(self, payload: dict[str, Any]) -> str:
        return self._write_json("wallets", payload)

    def save_signal(self, payload: dict[str, Any]) -> str:
        return self._write_json("signals", payload)

    def save_trade(self, payload: dict[str, Any]) -> str:
        target = self.exports_dir / "trades.csv"
        exists = target.exists()
        with target.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(payload.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(payload)
        return str(target)

    def _read_snapshots(self, folder: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.snapshot_dirs[folder].glob("*.json")):
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        return rows

    def get_markets(self) -> list[dict[str, Any]]:
        return self._read_snapshots("markets")

    def get_wallets(self) -> list[dict[str, Any]]:
        return self._read_snapshots("wallets")

    def get_signals(self) -> list[dict[str, Any]]:
        return self._read_snapshots("signals")

    def get_trades(self) -> list[dict[str, Any]]:
        target = self.exports_dir / "trades.csv"
        if not target.exists():
            return []
        with target.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def export_trades(self, path: str | None = None) -> str:
        target = Path(path) if path else self.exports_dir / "trades.csv"
        target.touch(exist_ok=True)
        return str(target)

    def export_signals(self, path: str | None = None) -> str:
        target = Path(path) if path else self.exports_dir / "signals.csv"
        signals = self.get_signals()
        if signals:
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(signals[0].keys()))
                writer.writeheader()
                writer.writerows(signals)
        else:
            target.touch(exist_ok=True)
        return str(target)
