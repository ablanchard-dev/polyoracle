from sqlmodel import SQLModel, Session, create_engine

from app.services.paper_trading_engine import PaperTradingEngine


def test_paper_trade_open_and_close() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        paper = PaperTradingEngine(session)
        trade = paper.open_paper_position("m1", "YES", "buy", 100, 0.40, 0.02)
        closed = paper.close_paper_position(trade, 0.55)

    assert closed.status == "closed"
    assert closed.realized_pnl > 0
