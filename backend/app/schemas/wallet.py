from datetime import datetime

from pydantic import BaseModel


class WalletRead(BaseModel):
    address: str
    score: float
    pnl: float
    roi: float
    win_rate: float
    market_count: int
    volume: float
    specialty: str | None
    last_trade_at: datetime | None
    status: str
