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


class EntryPriceAudit(SQLModel, table=True):
    """P0.4 (Round 8, 2026-05-12) — per-trade entry-price provenance audit.

    One-to-one with PaperTrade. Captures EXACTLY what the bot saw at the moment
    of `open_paper_position()`:
      - the raw source signal price (= wallet's trade price)
      - the live Gamma price the bot re-fetched at open time
      - the CLOB best bid/ask (if available)
      - the chosen entry price + source label
      - a confidence tag indicating live-executable certainty

    Why a dedicated table (not metadata in close_reason): close_reason is
    written at CLOSE time and may be reconstructed; we need provenance frozen
    at OPEN time. Dedicated table also makes future SQL/Pandas analysis cheap
    and avoids string-parsing the close_reason JSON.

    Used by:
      - M1 v5 visibility gate (P0.3) — gamma_clob_consistency, no SYNTH bias
      - E8 first-live calibration (P1) — measure real haircut vs paper
      - Future P4-C copyable_score — gamma_clob_consistency wallet metric
    """
    id: str = Field(primary_key=True)
    paper_trade_id: str = Field(index=True)
    market_id: str = Field(index=True)
    outcome: str
    side: str

    # Source provenance (the wallet trade we copied)
    source_trade_id: str | None = None
    source_wallet: str | None = None
    raw_signal_price: float | None = None

    # Live re-fetch at open time
    gamma_mid_at_open: float | None = None
    clob_best_bid_at_open: float | None = None
    clob_best_ask_at_open: float | None = None
    spread_at_open: float | None = None
    liquidity_score_at_open: float | None = None

    # What the bot actually used as entry
    chosen_entry_price: float = 0.0
    entry_price_source: str = "unknown"  # one of: GAMMA, CLOB, SYNTH, WALLET_FROZEN_FALLBACK, UNKNOWN
    price_source_confidence: str = "LOW"  # HIGH / MEDIUM / LOW

    # Live shadow projection: what would the live fill have been?
    live_shadow_entry_price: float | None = None  # best_ask for BUY, best_bid for SELL
    live_shadow_slippage_abs: float | None = None  # |live_shadow - chosen|
    live_shadow_slippage_pct: float | None = None  # / chosen

    # Timestamps
    opened_at: datetime = Field(index=True, default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
