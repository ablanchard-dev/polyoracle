from sqlmodel import SQLModel, Session, create_engine

from app.models.market import Market
from app.storage.sqlite_storage import SQLiteStorage


def test_sqlite_storage_reads_markets_and_exports(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Market(id="m1", question="Question?", volume_24h=1, volume_7d=2))
        session.commit()

        storage = SQLiteStorage(session, str(tmp_path / "polyoracle.db"))
        markets = storage.get_markets()
        export_path = storage.export_trades()

    assert markets[0]["id"] == "m1"
    assert export_path.endswith("trades.csv")
