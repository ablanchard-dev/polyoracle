from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class PaperTrade(SQLModel, table=True):
    id: str = Field(primary_key=True)
    market_id: str
    outcome: str
    side: str
    quantity: float
    average_price: float
    fees: float = 0.0
    slippage: float = 0.0
    realized_pnl: float = 0.0
    status: str = "open"
    opened_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None
    signal_id: str | None = None
    wallet_address: str | None = None
    notional_usd: float = 0.0
    auto: bool = False
    close_reason: str | None = None


class PublicTrade(SQLModel, table=True):
    id: str = Field(primary_key=True)
    wallet_address: str = Field(index=True)
    market_id: str = Field(index=True)
    outcome: str | None = None
    side: str | None = None
    price: float = 0.0
    size: float = 0.0
    notional_usd: float = 0.0
    traded_at: datetime | None = None
    data_source: str = "mock"
    seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TradeAuditRecord(SQLModel, table=True):
    id: str = Field(primary_key=True)
    trade_id: str = Field(index=True)
    wallet_address: str = Field(index=True)
    market_id: str = Field(index=True)
    question: str | None = None
    outcome: str | None = None
    side: str | None = None
    price: float = 0.0
    size: float = 0.0
    notional_usd: float = 0.0
    estimated_spread: float = 0.0
    estimated_slippage: float = 0.0
    wallet_score: float = 0.0
    wallet_tier: str = "INSUFFICIENT_DATA"
    market_liquidity_score: float = 0.0
    orderbook_quality: str = "INSUFFICIENT_DATA"
    copy_delay_seconds: float = 0.0
    price_deterioration: float = 0.0
    copyable_edge: float = 0.0
    trade_quality_score: float = 0.0
    decision: str = "IGNORE"
    reasons: str | None = None
    warnings: str | None = None
    audited_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TradeCluster(SQLModel, table=True):
    id: str = Field(primary_key=True)
    market_id: str = Field(index=True)
    outcome: str | None = None
    side: str | None = None
    wallet_count: int = 0
    trade_count: int = 0
    notional_usd: float = 0.0
    average_price: float = 0.0
    average_wallet_score: float = 0.0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    confidence: float = 0.0
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SmartMoneyEvent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    event_type: str
    wallet_address: str | None = None
    market_id: str | None = None
    outcome: str | None = None
    notional_usd: float = 0.0
    price: float = 0.0
    confidence: float = 0.0
    summary: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NoTradeDecision(SQLModel, table=True):
    id: str = Field(primary_key=True)
    signal_id: str | None = Field(default=None, index=True)
    market_id: str | None = Field(default=None, index=True)
    wallet_address: str | None = None
    reason_code: str
    details: str | None = None
    saved_loss_estimate: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
