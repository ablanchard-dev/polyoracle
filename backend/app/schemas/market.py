from datetime import datetime

from pydantic import BaseModel


class MarketRead(BaseModel):
    id: str
    question: str
    category: str | None
    yes_price: float | None
    no_price: float | None
    volume_24h: float
    volume_7d: float
    spread: float
    liquidity: float
    open_interest: float
    deadline: datetime | None
    opportunity_score: float
    clob_token_ids: str | None = None
    data_source: str = "mock"
    raw_slug: str | None = None
