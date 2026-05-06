from pydantic import BaseModel


class BotStatus(BaseModel):
    mode: str
    running: bool
    paused: bool
    kill_switch_active: bool
    live_enabled: bool
    live_blocked_reason: str | None = None
    paper_capital: float
    paper_pnl: float
    exposure: float
    open_positions: int
    active_signals: int
    last_error: str | None = None


class BotActionResult(BaseModel):
    status: BotStatus
    message: str
