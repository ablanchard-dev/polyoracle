from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class BotState(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    mode: str = "RESEARCH"
    running: bool = False
    paused: bool = False
    kill_switch_active: bool = False
    live_enabled: bool = False
    paper_capital: float = 10_000.0
    paper_pnl: float = 0.0
    exposure: float = 0.0
    open_positions: int = 0
    active_signals: int = 0
    last_error: str | None = None
    # v0.7.8 P6 — R-based dynamic sizing state machine.
    # 2.0 = "winning" posture (2R per trade = base × 2)
    # 1.0 = "losing" posture (1R per trade — anti-drawdown until next win)
    # Updated atomically on each close: realized_pnl > 0 → 2.0, else → 1.0.
    current_r_multiplier: float = 2.0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RiskModeState(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    mode: str = "SAFE"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_by: str | None = None


class BotLoopState(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    mode: str = "RESEARCH"
    running: bool = False
    paused: bool = False
    kill_switch_active: bool = False
    last_cycle_at: datetime | None = None
    last_cycle_duration_ms: float = 0.0
    cycles_count: int = 0
    errors_count: int = 0
    markets_scanned: int = 0
    wallets_audited: int = 0
    trades_audited: int = 0
    signals_generated: int = 0
    paper_trades_opened: int = 0
    rejected_signals: int = 0
    last_error: str | None = None
    last_cycle_summary: str | None = None
    polling_running: bool = False
    polling_started_at: datetime | None = None
    last_poll_at: datetime | None = None
    wallets_polled_count: int = 0
    trades_detected_total: int = 0
    paper_trades_via_polling: int = 0
    polling_errors_count: int = 0
    last_polling_error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
