from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class Market(SQLModel, table=True):
    id: str = Field(primary_key=True)
    question: str
    category: str | None = None
    yes_price: float | None = None
    no_price: float | None = None
    volume_24h: float = 0.0
    volume_7d: float = 0.0
    spread: float = 0.0
    liquidity: float = 0.0
    open_interest: float = 0.0
    deadline: datetime | None = None
    opportunity_score: float = 0.0
    clob_token_ids: str | None = None
    data_source: str = "mock"
    raw_slug: str | None = None
    active: bool = True
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # v0.7.8 P4 — derived fields for capital-turnover-aware allocation.
    # Backfilled from gamma `endDate` minus market.updated_at, then bucketed
    # via the same DURATION_BUCKETS constants as duration_filter.py.
    expected_resolution_minutes: float | None = None
    market_speed_class: str | None = None  # FAST / MEDIUM / SLOW
    duration_bucket: str | None = None     # ULTRA_SHORT/VERY_SHORT/SHORT/MEDIUM/LONG/VERY_LONG
