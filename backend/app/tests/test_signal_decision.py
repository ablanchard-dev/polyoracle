from sqlmodel import SQLModel, Session, create_engine

from app.models.trade import TradeAuditRecord
from app.services.signal_engine import SignalEngine


def _engine() -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_signal_engine_produces_paper_trade_for_strong_audit() -> None:
    engine = _engine()
    with Session(engine) as session:
        record = TradeAuditRecord(
            id="a1",
            trade_id="t1",
            wallet_address="0xelite",
            market_id="m1",
            outcome="YES",
            side="BUY",
            price=0.45,
            size=2_000,
            notional_usd=90_000,
            estimated_spread=0.01,
            estimated_slippage=0.005,
            wallet_score=88,
            wallet_tier="ELITE",
            market_liquidity_score=80,
            orderbook_quality="GOOD",
            copy_delay_seconds=20,
            price_deterioration=0.0,
            copyable_edge=0.04,
            trade_quality_score=82,
            decision="PAPER_TRADE",
        )
        session.add(record)
        session.commit()
        signal = SignalEngine(session).build_signal_from_trade_audit(record)
        decision = signal.decision
        score = signal.score
    assert decision in {"PAPER_TRADE", "WATCH"}
    assert score >= 0


def test_signal_engine_rejects_suspicious_wallet() -> None:
    engine = _engine()
    with Session(engine) as session:
        record = TradeAuditRecord(
            id="a2",
            trade_id="t2",
            wallet_address="0xsus",
            market_id="m1",
            outcome="YES",
            side="BUY",
            price=0.45,
            size=2_000,
            notional_usd=90_000,
            estimated_spread=0.01,
            estimated_slippage=0.005,
            wallet_score=20,
            wallet_tier="SUSPICIOUS",
            market_liquidity_score=80,
            orderbook_quality="GOOD",
            copy_delay_seconds=20,
            price_deterioration=0.0,
            copyable_edge=0.0,
            trade_quality_score=40,
            decision="IGNORE",
        )
        session.add(record)
        session.commit()
        signal = SignalEngine(session).build_signal_from_trade_audit(record)
        decision = signal.decision
    assert decision == "REJECT"
