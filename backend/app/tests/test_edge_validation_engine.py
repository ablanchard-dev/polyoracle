from sqlmodel import SQLModel, Session, create_engine

from app.services.edge_validation_engine import EdgeValidationEngine
from app.services.paper_trading_engine import PaperTradingEngine


def test_edge_report_stays_insufficient_with_small_sample() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        report = EdgeValidationEngine(session).generate_edge_report()

    assert report["conclusion"] == "INSUFFICIENT_DATA"
    assert "copyable_edge" in report["metrics"]


def test_edge_metrics_use_closed_paper_trades() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        paper = PaperTradingEngine(session)
        trade = paper.open_paper_position("m1", "YES", "buy", 100, 0.40, 0.01)
        paper.close_paper_position(trade, 0.55)
        metrics = EdgeValidationEngine(session).metrics()

    assert metrics.expectancy > 0
    assert metrics.profit_factor > 1
