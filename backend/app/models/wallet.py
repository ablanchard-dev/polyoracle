from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class Wallet(SQLModel, table=True):
    address: str = Field(primary_key=True)
    score: float = 0.0
    pnl: float = 0.0
    roi: float = 0.0
    win_rate: float = 0.0
    market_count: int = 0
    volume: float = 0.0
    specialty: str | None = None
    last_trade_at: datetime | None = None
    status: str = "watched"
    tier: str = "INSUFFICIENT_DATA"
    confidence: float = 0.0
    data_source: str = "mock"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WalletAudit(SQLModel, table=True):
    id: str = Field(primary_key=True)
    address: str = Field(index=True)
    audit_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    smart_score: float = 0.0
    whale_score: float = 0.0
    reliability: float = 0.0
    copyability: float = 0.0
    confidence: float = 0.0
    sample_size: int = 0
    tier: str = "INSUFFICIENT_DATA"
    specialty: str | None = None
    pnl: float = 0.0
    roi: float = 0.0
    win_rate: float = 0.0
    average_position_size: float = 0.0
    max_drawdown: float = 0.0
    suspicious: bool = False
    suspicious_reason: str | None = None
    data_source: str = "mock"
    notes: str | None = None


class WalletScoreSnapshot(SQLModel, table=True):
    id: str = Field(primary_key=True)
    address: str = Field(index=True)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    pnl_score: float = 0.0
    roi_score: float = 0.0
    sample_size_score: float = 0.0
    consistency_score: float = 0.0
    copyability_score: float = 0.0
    timing_score: float = 0.0
    exit_quality_score: float = 0.0
    category_specialization_score: float = 0.0
    liquidity_quality_score: float = 0.0
    risk_management_score: float = 0.0
    recent_activity_score: float = 0.0
    confidence_score: float = 0.0
    penalty_total: float = 0.0
    smart_score: float = 0.0


class WalletTradeRecord(SQLModel, table=True):
    id: str = Field(primary_key=True)
    address: str = Field(index=True)
    market_id: str | None = None
    outcome: str | None = None
    side: str | None = None
    price: float = 0.0
    size: float = 0.0
    notional_usd: float = 0.0
    traded_at: datetime | None = None
    data_source: str = "mock"


class WalletDailyStat(SQLModel, table=True):
    id: str = Field(primary_key=True)
    address: str = Field(index=True)
    day: str = Field(index=True)
    trades: int = 0
    notional_usd: float = 0.0
    realized_pnl: float = 0.0
    win_rate: float = 0.0


class WalletWatchEntry(SQLModel, table=True):
    address: str = Field(primary_key=True)
    list_type: str = "watch"
    reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WalletCategoryStat(SQLModel, table=True):
    id: str = Field(primary_key=True)
    address: str = Field(index=True)
    category: str
    trades: int = 0
    win_rate: float = 0.0
    edge: float = 0.0


class MarketFirstWalletRecord(SQLModel, table=True):
    address: str = Field(primary_key=True)
    market_first_score: float = 0.0
    composite_score: float = 0.0
    tier: str = "INSUFFICIENT_DATA"
    status: str = "IGNORE"
    candidate_status: str | None = None
    resolved_market_win_rate: float | None = None
    win_rate_confidence: str = "INSUFFICIENT_DATA"
    resolved_markets_traded: int = 0
    resolved_winning_markets: int = 0
    resolved_losing_markets: int = 0
    best_category: str | None = None
    category_win_rates: str | None = None
    recent_activity_score: float = 0.0
    copyability_score: float = 0.0
    total_resolved_notional: float = 0.0
    average_position_size: float = 0.0
    median_position_size: float = 0.0
    warnings: str | None = None
    reasons: str | None = None
    data_source: str = "polymarket_gamma+data"
    audit_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResolvedMarketRecord(SQLModel, table=True):
    market_id: str = Field(primary_key=True)
    condition_id: str | None = Field(default=None, index=True)
    question: str | None = None
    category: str | None = None
    end_date: str | None = None
    closed: bool = True
    winning_outcome_index: int | None = None
    winning_outcome_name: str | None = None
    holders_count: int = 0
    trades_count: int = 0
    usable: bool = False
    rejection_reason: str | None = None
    audited_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
