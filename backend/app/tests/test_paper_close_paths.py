"""v0.5.8 — close-path tests for PaperTradingEngine.

Verifies the 4 close paths the user explicitly required:

* Resolution close   — market resolves, position closes at 1.0/0.0
* Manual close       — close_position_by_id at given price
* Take profit / stop loss
* Max holding time
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.trade import PaperTrade
from app.models.wallet import ResolvedMarketRecord
from app.services.paper_trading_engine import PaperTradingEngine


@pytest.fixture()
def session(tmp_path: Path) -> Session:  # type: ignore[misc]
    """Build a fresh in-memory engine + session per test."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _open_position(session: Session, **overrides: object) -> PaperTrade:
    base = {
        "id": "tr-1",
        "market_id": "0xmarket",
        "outcome": "Yes",
        "side": "BUY",
        "quantity": 100.0,
        "average_price": 0.40,
        "status": "open",
        "opened_at": datetime.now(UTC) - timedelta(hours=1),
        "auto": True,
        "wallet_address": "0xelite",
    }
    base.update(overrides)
    trade = PaperTrade(**base)  # type: ignore[arg-type]
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _add_resolution(
    session: Session,
    market_id: str,
    *,
    winning_outcome_index: int,
    winning_outcome_name: str,
    condition_id: str | None = None,
) -> None:
    rec = ResolvedMarketRecord(
        market_id=market_id,
        condition_id=condition_id or market_id,
        question="Test",
        category="Politics",
        end_date="2024-06-15",
        closed=True,
        winning_outcome_index=winning_outcome_index,
        winning_outcome_name=winning_outcome_name,
        usable=True,
    )
    session.add(rec)
    session.commit()


def test_close_position_on_market_resolution_winner(session: Session) -> None:
    """v0.7.8 — assertion now reflects NET PnL after B12 entry fee.
    Resolution closes do NOT charge an exit fee (oracle-settled).
    Default category fee (no Market row in test) = 5%.
    Entry fee = 100 × 0.05 × 0.40 × 0.60 = 1.20.
    Net = (1.0 − 0.40) × 100 − 1.20 = 58.80."""
    trade = _open_position(session, outcome="Yes")
    _add_resolution(session, "0xmarket", winning_outcome_index=0, winning_outcome_name="Yes")
    engine = PaperTradingEngine(session)
    events = engine.evaluate_open_positions()
    assert len(events) == 1
    assert events[0]["action"] == "market_resolved"
    assert events[0]["exit"] == 1.0
    closed = session.get(PaperTrade, trade.id)
    assert closed.status == "closed"
    expected_gross = (1.0 - 0.40) * 100.0
    expected_entry_fee = 100.0 * 0.05 * 0.40 * 0.60  # default rate, no exit fee on resolve
    assert closed.realized_pnl == round(expected_gross - expected_entry_fee, 2)
    assert closed.close_reason == "market_resolved"


def test_close_position_on_market_resolution_loser(session: Session) -> None:
    """v0.7.8 — net PnL after entry fee. No exit fee on resolution."""
    trade = _open_position(session, outcome="Yes")
    _add_resolution(session, "0xmarket", winning_outcome_index=1, winning_outcome_name="No")
    engine = PaperTradingEngine(session)
    events = engine.evaluate_open_positions()
    assert events[0]["exit"] == 0.0
    closed = session.get(PaperTrade, trade.id)
    expected_gross = (0.0 - 0.40) * 100.0
    expected_entry_fee = 100.0 * 0.05 * 0.40 * 0.60
    assert closed.realized_pnl == round(expected_gross - expected_entry_fee, 2)
    assert closed.close_reason == "market_resolved"


def test_realized_pnl_calculation_correct_sell_side(session: Session) -> None:
    """SELL side: profit when price drops below entry. v0.7.8 — fees on
    entry only when resolution closes."""
    trade = _open_position(session, side="SELL", outcome="No")
    _add_resolution(session, "0xmarket", winning_outcome_index=0, winning_outcome_name="Yes")
    engine = PaperTradingEngine(session)
    engine.evaluate_open_positions()
    closed = session.get(PaperTrade, trade.id)
    # SELL @ 0.40 → outcome "No" loses (winner is "Yes") → exit at 0.0.
    # Gross SELL pnl = (entry − exit) × qty = (0.40 − 0.0) × 100 = 40.
    expected_entry_fee = 100.0 * 0.05 * 0.40 * 0.60
    assert closed.realized_pnl == round(40.0 - expected_entry_fee, 2)


def test_manual_close_via_close_position_by_id(session: Session) -> None:
    trade = _open_position(session)
    engine = PaperTradingEngine(session)
    closed = engine.close_position_by_id(trade.id, exit_price=0.55, reason="manual")
    assert closed is not None
    assert closed.status == "closed"
    assert closed.close_reason == "manual"
    # v0.7.8 — manual close charges BOTH entry and exit fees (mid-market exit,
    # not oracle-settled). Default rate 5%, qty 100, entry 0.40, exit 0.55.
    expected_gross = (0.55 - 0.40) * 100.0
    expected_entry_fee = 100.0 * 0.05 * 0.40 * 0.60
    expected_exit_fee = 100.0 * 0.05 * 0.55 * 0.45
    assert closed.realized_pnl == round(expected_gross - expected_entry_fee - expected_exit_fee, 2)


def test_take_profit_triggers_at_10pct(session: Session) -> None:
    trade = _open_position(session, average_price=0.50)
    engine = PaperTradingEngine(session)
    events = engine.evaluate_open_positions(price_lookup={trade.market_id: 0.56})
    assert len(events) == 1 and events[0]["action"] == "take_profit"


def test_stop_loss_triggers_at_minus_5pct(session: Session) -> None:
    trade = _open_position(session, average_price=0.50)
    engine = PaperTradingEngine(session)
    events = engine.evaluate_open_positions(price_lookup={trade.market_id: 0.475})
    assert events and events[0]["action"] == "stop_loss"


def test_max_holding_time_closes_after_7_days(session: Session) -> None:
    old_open = datetime.now(UTC) - timedelta(days=8)
    trade = _open_position(session, opened_at=old_open)
    engine = PaperTradingEngine(session)
    events = engine.evaluate_open_positions(price_lookup={trade.market_id: 0.41})
    assert events and events[0]["action"] == "max_holding_time"


def test_max_holding_time_closes_even_without_price_lookup(session: Session) -> None:
    old_open = datetime.now(UTC) - timedelta(days=8)
    trade = _open_position(session, opened_at=old_open)
    engine = PaperTradingEngine(session)
    events = engine.evaluate_open_positions()  # no price_lookup
    assert events and events[0]["action"] == "max_holding_time_no_price"


def test_resolution_close_beats_take_profit(session: Session) -> None:
    """If a market resolves AND price was already in TP zone, resolution wins."""
    trade = _open_position(session, average_price=0.40, outcome="Yes")
    _add_resolution(session, "0xmarket", winning_outcome_index=1, winning_outcome_name="No")
    engine = PaperTradingEngine(session)
    events = engine.evaluate_open_positions(price_lookup={trade.market_id: 0.95})
    assert events and events[0]["action"] == "market_resolved"
    closed = session.get(PaperTrade, trade.id)
    # outcome "Yes" loses → exit 0.0 → gross = (0 − 0.4) × 100 = −40.
    # v0.7.8 — minus entry fee (resolution close = no exit fee).
    expected_entry_fee = 100.0 * 0.05 * 0.40 * 0.60
    assert closed.realized_pnl == round(-40.0 - expected_entry_fee, 2)


def test_resolution_uses_condition_id_fallback(session: Session) -> None:
    """ResolvedMarketRecord may key by market_id or condition_id."""
    trade = _open_position(session, market_id="0xcondID-only", outcome="Yes")
    _add_resolution(
        session,
        "0xinternal-id",
        winning_outcome_index=0,
        winning_outcome_name="Yes",
        condition_id="0xcondID-only",
    )
    engine = PaperTradingEngine(session)
    events = engine.evaluate_open_positions()
    assert events and events[0]["action"] == "market_resolved"
