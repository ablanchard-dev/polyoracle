"""A-T0 strict_cutover marker tests (2026-05-11).

Tests directs sur la logique cutover sans TestClient (évite les problèmes
de session SQLAlchemy entre fixture et FastAPI Depends). Le route
/bot/strict-cutover wrappe simplement la même logique avec session
injection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

# Import all models so SQLModel.metadata.create_all picks them up
from app.models.audit import AuditEvent  # noqa: F401 — required for table creation
from app.models.bot import BotState
from app.models.trade import PaperTrade


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(BotState(id=1, mode="PAPER", running=True, paper_capital=219.10))
        s.commit()
        yield s


def _do_cutover(session: Session) -> dict:
    """Mirror logic from POST /bot/strict-cutover route."""
    from app.services.audit_logger import AuditLogger
    state = session.get(BotState, 1)
    assert state is not None
    now = datetime.now(UTC)
    state.strict_cutover_at = now
    state.updated_at = now
    session.add(state)
    AuditLogger(session).log("STRICT_CUTOVER", state.mode, result=f"cutover_at={now.isoformat()}")
    session.commit()
    return {
        "strict_cutover_at": now.isoformat(),
        "paper_capital_preserved": state.paper_capital,
    }


def _get_cutover_status(session: Session) -> dict:
    """Mirror logic from GET /bot/strict-cutover route."""
    state = session.get(BotState, 1)
    if state is None or state.strict_cutover_at is None:
        return {"strict_cutover_at": None, "trades_after_cutover": 0}
    cutover = state.strict_cutover_at
    if cutover.tzinfo is None:
        cutover = cutover.replace(tzinfo=UTC)
    after = list(session.exec(
        select(PaperTrade).where(PaperTrade.opened_at >= cutover)
    ).all())
    return {
        "strict_cutover_at": cutover.isoformat(),
        "trades_after_cutover": len(after),
        "paper_capital": state.paper_capital,
    }


def test_strict_cutover_sets_marker(session: Session):
    body = _do_cutover(session)
    assert body["strict_cutover_at"] is not None
    assert body["paper_capital_preserved"] == 219.10  # capital NOT reset

    state = session.get(BotState, 1)
    assert state.strict_cutover_at is not None
    assert state.paper_capital == 219.10  # capital preserved


def test_strict_cutover_idempotent_overwrites_timestamp(session: Session):
    body1 = _do_cutover(session)
    body2 = _do_cutover(session)
    assert body1["strict_cutover_at"] != body2["strict_cutover_at"]
    # Capital still preserved
    assert body2["paper_capital_preserved"] == 219.10


def test_get_cutover_status_no_marker(session: Session):
    status = _get_cutover_status(session)
    assert status["strict_cutover_at"] is None
    assert status["trades_after_cutover"] == 0


def test_get_cutover_status_counts_trades_after(session: Session):
    # Insert old trade BEFORE cutover
    old_open = datetime.now(UTC) - timedelta(hours=2)
    session.add(PaperTrade(
        id="legacy-1", market_id="0xa", outcome="Yes", side="BUY",
        quantity=10, average_price=0.5, status="closed", opened_at=old_open,
    ))
    session.commit()

    _do_cutover(session)

    # Insert new trade AFTER cutover
    new_open = datetime.now(UTC) + timedelta(seconds=2)
    session.add(PaperTrade(
        id="strict-1", market_id="0xb", outcome="Yes", side="BUY",
        quantity=10, average_price=0.5, status="open", opened_at=new_open,
    ))
    session.commit()

    status = _get_cutover_status(session)
    assert status["trades_after_cutover"] == 1
    assert status["paper_capital"] == 219.10
