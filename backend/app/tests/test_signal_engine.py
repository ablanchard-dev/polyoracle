from sqlmodel import Session, create_engine

from app.services.signal_engine import SignalEngine


def test_signal_engine_rejects_bad_signal() -> None:
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        signal = SignalEngine(session).generate_signals()[-1]

    assert SignalEngine(session).reject_bad_signal(signal) is True
