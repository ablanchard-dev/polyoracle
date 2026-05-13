from functools import lru_cache
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BotMode = Literal["OFF", "RESEARCH", "PAPER", "SEMI_AUTO", "LIVE"]


# v0.7.8 — auto-drop forbidden OS env DATABASE_URL leaks. Some operators
# carry a user-level DATABASE_URL from a sibling project (e.g. Lumenia
# under /Downloads/) which would silently win over .env. This guard pops
# the OS env at module import time when its value matches a forbidden
# pattern, so the project's .env value applies instead. The backend's
# _validate_database_url() in database.py keeps the second-line guard.
def _drop_leaked_os_database_url() -> None:
    import os
    leaked = os.environ.get("DATABASE_URL", "") or ""
    forbidden = ("lumenia", "/downloads/", "\\downloads\\", "/temp/", "\\temp\\")
    if leaked and any(p in leaked.lower() for p in forbidden):
        os.environ.pop("DATABASE_URL", None)


_drop_leaked_os_database_url()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    local_first: bool = True
    storage_backend: Literal["sqlite", "file", "duckdb", "postgres"] = "sqlite"
    sqlite_path: str = "./data/polyoracle.db"
    duckdb_enabled: bool = False
    postgres_enabled: bool = False
    redis_enabled: bool = False
    paper_trading_enabled: bool = True
    # P0/Phase A 2026-05-11: when True, paper applies EXACTLY the same gates
    # as live (spread/liquidity/orderbook/copyable_edge/unknown-category).
    # Defaults False for safe rollout — flip to True to neutralize the
    # _elite_paper_bypass and the risk_engine ELITE+paper exceptions.
    paper_live_strict: bool = False
    # Phase G ledger (2026-05-13): WalletMarketResolutionAudit table now in
    # place (unique wallet+market+outcome). The runtime hook in
    # _close_paper_with_reason()->_b22_runtime_update_mfwr_on_resolved_close()
    # is safe again:
    #   - Inserts a ledger row first (idempotent via UNIQUE constraint)
    #   - Increments MFWR W/L ONLY on insert success
    #   - audit_at is NEVER touched (only batch B22 advances it)
    # Re-enabled by default. Set False to disable for diagnostics.
    enable_b22_runtime_hook: bool = True
    mock_data_enabled: bool = True
    polymarket_public_enabled: bool = True
    market_fetch_limit: int = 100
    orderbook_snapshot_enabled: bool = True

    database_url: str | None = None
    redis_url: str = "redis://redis:6379/0"

    gamma_api_base_url: str = "https://gamma-api.polymarket.com"
    data_api_base_url: str = "https://data-api.polymarket.com"
    clob_api_base_url: str = "https://clob.polymarket.com"

    live_enabled: bool = False
    default_bot_mode: BotMode = "RESEARCH"
    compliance_jurisdiction: str = "UNSET"
    allowed_live_jurisdictions: list[str] = Field(default_factory=list)

    paper_starting_capital: float = 10_000.0
    max_risk_per_trade: float = 0.01
    max_total_exposure: float = 0.15
    max_daily_loss: float = 0.03
    max_weekly_loss: float = 0.08
    min_signal_score: float = 70.0
    max_spread: float = 0.05
    min_liquidity: float = 1_000.0

    # v0.4 paper trading auto controls
    paper_capital: float = 1_000.0
    paper_max_risk_per_trade: float = 0.01
    paper_max_exposure: float = 0.20
    paper_max_market_exposure: float = 0.05
    paper_max_daily_loss: float = 0.03
    paper_max_weekly_loss: float = 0.08
    min_confidence_score: float = 60.0
    min_copyable_edge: float = 60.0
    max_spread_pct: float = 0.03
    min_liquidity_score: float = 50.0
    auto_paper_trade_enabled: bool = True

    # v0.4 bot loop tuning
    top_wallets_target: int = 100
    top_wallets_min: int = 50
    max_wallet_trades_per_audit: int = 500
    max_markets_per_cycle: int = 100
    max_trades_per_cycle: int = 1000
    audit_interval_seconds: int = 120
    wallet_refresh_interval_minutes: int = 30
    market_scan_interval_seconds: int = 60
    orderbook_fetch_limit: int = 50
    trade_audit_enabled: bool = True
    auto_watchlist_enabled: bool = True

    # v0.5 market-first discovery
    market_first_discovery_enabled: bool = True
    market_first_days_back: int = 90
    market_first_market_limit: int = 300
    market_first_holders_limit: int = 100
    market_first_max_markets_per_run: int = 100
    market_first_sleep_ms_between_calls: int = 250
    market_first_require_resolved: bool = True
    market_first_min_sample_for_strong: int = 30
    market_first_min_sample_for_watch: int = 10
    market_first_trades_per_market_limit: int = 1000

    # v0.7.2 — risk_mode is a legacy persisted setting. The runtime no
    # longer routes by named modes (SAFE / AGGRESSIVE / FULL_PAPER); the
    # CapitalAllocator + capital-aware sliding scale handle selectivity.
    # The default stays as the historic value so existing DBs keep loading,
    # but the value is ignored by paper_trading_engine in v0.7.2+.
    risk_mode: Literal["SAFE", "AGGRESSIVE", "FULL_PAPER"] = "SAFE"

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @property
    def resolved_sqlite_path(self) -> Path:
        raw_path = Path(self.sqlite_path)
        if raw_path.is_absolute():
            return raw_path
        cwd = Path.cwd()
        if cwd.name == "backend":
            return cwd.parent / raw_path
        return cwd / raw_path

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.resolved_sqlite_path.as_posix()}"

    @field_validator("allowed_live_jurisdictions", "cors_origins", mode="before")
    @classmethod
    def split_csv(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return list(json.loads(stripped))
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
