from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings

router = APIRouter(tags=["settings"])


class SettingsRead(BaseModel):
    local_first: bool
    storage_backend: str
    sqlite_path: str
    duckdb_enabled: bool
    postgres_enabled: bool
    redis_enabled: bool
    live_enabled: bool
    paper_trading_enabled: bool
    mock_data_enabled: bool
    max_risk_per_trade: float
    max_daily_loss: float
    max_weekly_loss: float
    max_spread: float
    min_liquidity: float
    min_signal_score: float


class SettingsUpdate(BaseModel):
    paper_starting_capital: float | None = None
    max_risk_per_trade: float | None = None
    max_daily_loss: float | None = None
    max_weekly_loss: float | None = None
    max_spread: float | None = None
    min_liquidity: float | None = None
    min_signal_score: float | None = None


@router.get("/settings", response_model=SettingsRead)
def get_settings_route() -> SettingsRead:
    settings = get_settings()
    return SettingsRead(
        local_first=settings.local_first,
        storage_backend=settings.storage_backend,
        sqlite_path=settings.sqlite_path,
        duckdb_enabled=settings.duckdb_enabled,
        postgres_enabled=settings.postgres_enabled,
        redis_enabled=settings.redis_enabled,
        live_enabled=settings.live_enabled,
        paper_trading_enabled=settings.paper_trading_enabled,
        mock_data_enabled=settings.mock_data_enabled,
        max_risk_per_trade=settings.max_risk_per_trade,
        max_daily_loss=settings.max_daily_loss,
        max_weekly_loss=settings.max_weekly_loss,
        max_spread=settings.max_spread,
        min_liquidity=settings.min_liquidity,
        min_signal_score=settings.min_signal_score,
    )


@router.post("/settings", response_model=SettingsRead)
def update_settings_route(payload: SettingsUpdate) -> SettingsRead:
    # v0.2 keeps settings local and explicit. Persistence will be added via a local settings table.
    return get_settings_route()
