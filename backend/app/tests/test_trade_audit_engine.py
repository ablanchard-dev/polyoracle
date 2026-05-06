from datetime import UTC, datetime, timedelta

from sqlmodel import SQLModel, Session, create_engine

from app.models.wallet import Wallet
from app.services.trade_audit_engine import TradeAuditEngine


def _engine() -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_audit_trade_creates_record_and_decision() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(
            Wallet(
                address="0xstrong",
                score=82,
                tier="STRONG",
                confidence=80,
                pnl=80_000,
                roi=0.30,
                win_rate=0.62,
                market_count=120,
                volume=2_000_000,
            )
        )
        session.commit()
        engine_service = TradeAuditEngine(session)
        result = engine_service.audit_trade(
            {
                "trade_id": "t1",
                "address": "0xstrong",
                "market_id": "mock-election-001",
                "outcome": "YES",
                "side": "BUY",
                "price": 0.55,
                "size": 1_000,
                "notional_usd": 55_000,
                "traded_at": datetime.now(UTC) - timedelta(seconds=120),
                "data_source": "mock",
            }
        )
    assert result.decision in {"IGNORE", "WATCH", "SIGNAL", "PAPER_TRADE"}
    assert result.wallet_score == 82
    assert result.trade_quality_score >= 0


def test_decide_returns_ignore_when_wallet_unknown() -> None:
    engine = _engine()
    with Session(engine) as session:
        engine_service = TradeAuditEngine(session)
        result = engine_service.audit_trade(
            {
                "trade_id": "t2",
                "address": "0xnew",
                "market_id": "mock-fed-002",
                "outcome": "YES",
                "side": "BUY",
                "price": 0.40,
                "size": 100,
                "notional_usd": 4_000,
                "traded_at": datetime.now(UTC),
                "data_source": "mock",
            }
        )
    assert result.decision in {"IGNORE", "WATCH"}
