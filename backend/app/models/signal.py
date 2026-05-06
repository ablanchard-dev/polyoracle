from datetime import UTC, datetime
from enum import Enum

from sqlmodel import Field, SQLModel


class SignalType(str, Enum):
    SMART_WALLET_ENTRY = "SMART_WALLET_ENTRY"
    WHALE_ENTRY = "WHALE_ENTRY"
    SMART_CONSENSUS = "SMART_CONSENSUS"
    REPEATED_ACCUMULATION = "REPEATED_ACCUMULATION"
    MULTI_WALLET_CLUSTER = "MULTI_WALLET_CLUSTER"
    VOLUME_BREAKOUT = "VOLUME_BREAKOUT"
    ORDERBOOK_IMBALANCE = "ORDERBOOK_IMBALANCE"
    LIQUIDITY_SHIFT = "LIQUIDITY_SHIFT"
    EXIT_ALERT = "EXIT_ALERT"
    LATE_ENTRY_RISK = "LATE_ENTRY_RISK"
    LATE_CROWD_TRAP = "LATE_CROWD_TRAP"
    CONTRARIAN_ALERT = "CONTRARIAN_ALERT"
    WATCH_ONLY = "WATCH_ONLY"
    REJECTED = "REJECTED"


class Signal(SQLModel, table=True):
    id: str = Field(primary_key=True)
    market_id: str
    outcome: str
    signal_type: SignalType
    score: float
    confidence: float
    reason: str
    action: str = "watch"
    status: str = "watch"
    decision: str = "WATCH"
    wallets: str | None = None
    cluster_id: str | None = None
    proposed_size_usd: float = 0.0
    copyable_edge: float = 0.0
    notes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
