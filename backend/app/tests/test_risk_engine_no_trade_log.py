from sqlmodel import SQLModel, Session, create_engine

from app.services.risk_engine import RiskDecision, RiskEngine


def test_risk_logs_no_trade_with_reason_code() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        decision = RiskEngine().validate_trade(signal_score=20, spread=0.01, liquidity=10_000, capital=10_000)
        assert decision.approved is False
        assert decision.reason_code == "LOW_SIGNAL_SCORE"
        record = RiskEngine().log_no_trade(session, decision, signal_id="sig-1", market_id="m1")
        assert record.reason_code == "LOW_SIGNAL_SCORE"
        history = RiskEngine.list_no_trade_decisions(session)
    assert any(entry.signal_id == "sig-1" for entry in history)


def test_risk_check_orderbook_quality_handles_bad() -> None:
    decision = RiskEngine().check_orderbook_quality("BAD")
    assert decision.approved is False
    assert decision.reason_code == "BAD_ORDERBOOK"
