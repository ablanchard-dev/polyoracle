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
    # 2026-05-14 — doctrine opérateur review Round 9 :
    # PAPER_LIVE_STRICT = vrai par défaut, inviolable en prod/VPS. Paper
    # applique EXACTEMENT les mêmes gates que live (spread/liquidity/
    # orderbook/copyable_edge/unknown-category). Le bypass orderbook dans
    # TradeAuditEngine._decide() et _elite_paper_bypass dans
    # capital_allocator/risk_engine ne fire QUE si TOUTES les conditions
    # ci-dessous sont alignées :
    #   - paper_trading_enabled=True
    #   - live_enabled=False
    #   - paper_live_strict=False  ← interdit en prod/VPS, voir model_post_init
    #   - allow_non_strict_paper_research=True  ← opt-in explicite research
    # Default historique (False) était dangereux : config.py shippait avec
    # le bypass implicitement actif.
    paper_live_strict: bool = True
    # Opt-in research offline uniquement. Combiné avec paper_live_strict=False
    # et app_env != prod/vps, autorise le bypass orderbook paper-mode pour
    # explorer la cohort sans CLOB. JAMAIS en prod (bloqué par
    # model_post_init).
    allow_non_strict_paper_research: bool = False
    # Phase G ledger (2026-05-13): WalletMarketResolutionAudit table now in
    # place (unique wallet+market+outcome). The runtime hook in
    # _close_paper_with_reason()->_b22_runtime_update_mfwr_on_resolved_close()
    # is safe again:
    #   - Inserts a ledger row first (idempotent via UNIQUE constraint)
    #   - Increments MFWR W/L ONLY on insert success
    #   - audit_at is NEVER touched (only batch B22 advances it)
    # Re-enabled by default. Set False to disable for diagnostics.
    enable_b22_runtime_hook: bool = True

    # 2026-05-14 CLOB retry queue (Round 9 review strict-compatible fix).
    # Quand orderbook BAD/UNTRADABLE bloque un audit ELITE strong + tq>=70 +
    # liq>0, on enqueue dans PendingClobRetry au lieu de rejeter. Le worker
    # re-test CLOB toutes les 10-180s, et n'ouvre paper QUE si tous les gates
    # strict passent au moment du retry. Drill 2026-05-14 montre 42% des
    # candidats récupérables (CLOB book OK quelques min après l'audit).
    # clob_retry_auto_open_paper=False = phase 1 collecte+metric uniquement
    # (zéro risque). Flip à True après validation 1h pour activer re-open.
    clob_retry_enabled: bool = True
    clob_retry_auto_open_paper: bool = False
    clob_retry_max_attempts: int = 5
    clob_retry_batch_size: int = 20
    clob_retry_max_age_seconds: int = 300
    clob_retry_worker_interval_seconds: int = 5
    # Hard cap notional per phase 2 trade. Le worker ouvre des positions
    # avec pending.notional_usd qui est le notional du wallet SOURCE (peut
    # être un whale à $9000+). Ce cap protège l'exposure du bot.
    # 2026-05-14 16:27 — abaissé $20 → $5 : MaybeAutoTrade lit
    # paper_capital=100€ (BotState NANO baseline), pas effective_capital. Cap
    # exposure NANO = 0.75 × 100 = $75. À $20/trade phase 2, ~3-4 positions
    # saturaient le budget et bloquaient 1048 baseline PAPER_TRADE audits en
    # TOO_MUCH_EXPOSURE en 4h30. $5 matche le R-sizing NANO (2R ≈ $4) et
    # permet 15 phase 2 + baseline en parallèle.
    clob_retry_max_trade_notional_usd: float = 5.0
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

    def model_post_init(self, __context) -> None:
        # 2026-05-14 — doctrine paper = live strict (opérateur).
        # Refuser startup si on tourne en prod/VPS avec paper non-strict.
        # Empêche un setup permissif accidentel sur le VPS Hostinger.
        prod_envs = {"production", "prod", "vps", "vps_prod"}
        is_prod = (self.app_env or "").lower() in prod_envs
        if is_prod and not self.live_enabled and not self.paper_live_strict:
            raise ValueError(
                f"PAPER_LIVE_STRICT=false interdit en app_env={self.app_env!r}. "
                "Paper doit rester live-strict en prod/VPS (= paper = live truth). "
                "Pour research offline, set app_env=development AND "
                "ALLOW_NON_STRICT_PAPER_RESEARCH=true sur poste local."
            )
        # En dev, on permet strict=False uniquement si opt-in explicite.
        # Empêche oubli d'un flag .env qui activerait le bypass sans le savoir.
        if not self.paper_live_strict and not self.allow_non_strict_paper_research:
            raise ValueError(
                "PAPER_LIVE_STRICT=false nécessite ALLOW_NON_STRICT_PAPER_RESEARCH=true "
                "(opt-in research offline explicite). Sans ce flag, paper reste strict."
            )

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
