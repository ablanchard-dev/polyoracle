from sqlmodel import SQLModel, Session, create_engine

from app.models.bot import BotState
from app.services.execution_engine import ExecutionEngine


def test_bot_start_pause_stop_transitions() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        execution = ExecutionEngine(session)
        started = execution.start().status
        paused = execution.pause().status
        stopped = execution.stop().status

    assert started.running is True
    assert paused.paused is True
    assert stopped.running is False


def test_kill_switch_blocks_restart_message() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        execution = ExecutionEngine(session)
        execution.emergency_stop()
        result = execution.start()

    assert result.status.kill_switch_active is True
    assert "Kill switch active" in result.message
