from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class AuditEvent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    event_type: str
    bot_mode: str
    market_id: str | None = None
    outcome: str | None = None
    price: float | None = None
    signal_id: str | None = None
    score: float | None = None
    reason: str | None = None
    risk_decision: str | None = None
    order_payload: str | None = None
    result: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
