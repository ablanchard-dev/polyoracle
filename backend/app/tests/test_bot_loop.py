from sqlmodel import SQLModel, Session, create_engine

from app.services.bot_loop import BotLoop


def _engine() -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_bot_loop_run_once_in_research_mode_does_not_open_paper() -> None:
    engine = _engine()
    with Session(engine) as session:
        loop = BotLoop(session)
        loop.set_mode("RESEARCH")
        loop.start()
        result = loop.run_once(force=True)
    assert "duration_ms" in result
    assert result["paper_trades_opened"] == 0


def test_bot_loop_kill_switch_blocks_run() -> None:
    engine = _engine()
    with Session(engine) as session:
        loop = BotLoop(session)
        loop.kill_switch()
        result = loop.run_once()
    assert result["kill_switch_active"] is True
    assert result.get("message") == "kill switch active"


def test_bot_loop_rejects_live_mode() -> None:
    engine = _engine()
    with Session(engine) as session:
        loop = BotLoop(session)
        try:
            loop.set_mode("LIVE")
        except PermissionError as exc:
            assert "LIVE" in str(exc)
        else:
            raise AssertionError("LIVE mode must be blocked")
