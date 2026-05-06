from pathlib import Path

from sqlmodel import Session

from app.config import get_settings
from app.storage.file_storage import FileStorage
from app.storage.sqlite_storage import SQLiteStorage


class StorageService:
    def __init__(self, session: Session | None = None) -> None:
        self.settings = get_settings()
        self.session = session
        FileStorage(str(self.settings.resolved_sqlite_path.parent)).ensure_layout()

    def provider(self):
        if self.settings.storage_backend == "sqlite" and self.session is not None:
            return SQLiteStorage(self.session, str(self.settings.resolved_sqlite_path))
        return FileStorage(str(self.settings.resolved_sqlite_path.parent))

    def status(self) -> dict:
        data_root = self.settings.resolved_sqlite_path.parent
        return {
            "local_first": self.settings.local_first,
            "storage_backend": self.settings.storage_backend,
            "sqlite_path": str(self.settings.resolved_sqlite_path),
            "sqlite_exists": self.settings.resolved_sqlite_path.exists(),
            "data_root": str(data_root),
            "data_root_exists": data_root.exists(),
            "duckdb_enabled": self.settings.duckdb_enabled,
            "postgres_enabled": self.settings.postgres_enabled,
            "redis_enabled": self.settings.redis_enabled,
            "mock_data_enabled": self.settings.mock_data_enabled,
            "paper_trading_enabled": self.settings.paper_trading_enabled,
        }
