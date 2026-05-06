from datetime import datetime

from pydantic import BaseModel


class SignalRead(BaseModel):
    id: str
    market_id: str
    outcome: str
    signal_type: str
    score: float
    confidence: float
    reason: str
    action: str
    status: str
    created_at: datetime
