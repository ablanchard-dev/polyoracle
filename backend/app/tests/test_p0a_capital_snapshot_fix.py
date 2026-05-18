"""P0-A 2026-05-18 — fix bug #70 : exposure cap stale (self.paper_capital cached).

Bug context
===========

PaperTradingEngine.__init__ cached `self.paper_capital = self._effective_paper_capital()`
at engine creation. All downstream decision code (exposure ratios, max_wallet_usd,
max_market_usd, PortfolioState.capital_total, _capital_available_usd) read this
cached value forever.

In a long-lived engine (boot to hours later), even though _live_capital()
(sizing R) read fresh effective capital, the EXPOSURE CAP path stayed stuck
on the init-time value → bot blocked in 1-in/1-out at ~$10K exposure while
effective capital grew to $67K+.

Fix
===

In `maybe_auto_trade`, refresh `self.paper_capital = self._effective_paper_capital()`
at decision time so all downstream usages see fresh effective.

Plus new helper `_decision_capital_snapshot()` returning structured snapshot
for observability (logged in no-trade details).

Tests below verify the contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.bot import BotState
from app.models.trade import PaperTrade
from app.services.baseline_constants import EFFECTIVE_BASELINE_T0
from app.services.paper_trading_engine import PaperTradingEngine


@pytest.fixture
def clean_settings():
    """Patch get_settings to neutral test values (no env pollution).

    Real VPS .env has STRICT_CUTOVER_AT=2026-05-17 set, which filters out
    our test trades dated EFFECTIVE_BASELINE_T0. Tests must isolate from that.
    """
    with patch("app.services.paper_trading_engine.get_settings") as gs:
        gs.return_value.paper_live_strict = False
        gs.return_value.strict_cutover_at = None
        gs.return_value.live_enabled = False
        gs.return_value.paper_capital = 100.0
        yield gs


@pytest.fixture
def session_with_pnl(clean_settings):
    """Engine with paper_base=10000, plus 57300 cumul PnL via closed trades."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(BotState(id=1, paper_capital=10000.0, current_r_multiplier=2.0))
        # Add closed trades post-baseline summing to $57,300 realized PnL
        for i in range(10):
            s.add(PaperTrade(
                id=f"pnl-{i}",
                market_id="0xm",
                outcome="Yes",
                side="BUY",
                quantity=1.0,
                average_price=0.5,
                realized_pnl=5730.0,
                status="closed",
                opened_at=EFFECTIVE_BASELINE_T0 + timedelta(hours=i),
                closed_at=EFFECTIVE_BASELINE_T0 + timedelta(hours=i, minutes=10),
            ))
        s.commit()
        yield s


def test_snapshot_returns_fresh_effective_capital(session_with_pnl):
    """paper_base=$10K + PnL=$57.3K => effective ≈ $67.3K"""
    engine = PaperTradingEngine(session_with_pnl)
    snap = engine._decision_capital_snapshot()
    assert snap["paper_base"] == 10000.0
    assert snap["effective_capital"] == pytest.approx(67300.0, rel=0.001)


def test_snapshot_resolves_huge_tier_at_67k(session_with_pnl):
    """effective $67K falls in HUGE tier (64K-127.99K, max_pos 960, expo 95%)."""
    engine = PaperTradingEngine(session_with_pnl)
    snap = engine._decision_capital_snapshot()
    assert snap["tier_name"] == "HUGE"
    assert snap["max_positions_cap"] == 960
    assert snap["max_total_exposure_pct"] == pytest.approx(0.95)
    # max_total_exposure_usd ≈ 0.95 × 67300 ≈ 63935
    assert snap["max_total_exposure_usd"] == pytest.approx(63935.0, rel=0.001)


def test_snapshot_includes_open_notional_and_capital_available(session_with_pnl):
    """Snapshot includes open_notional_usd + capital_available."""
    # Add 2 OPEN trades with notional $5K each
    for i in range(2):
        session_with_pnl.add(PaperTrade(
            id=f"open-{i}",
            market_id=f"0xopen{i}",
            outcome="Yes",
            side="BUY",
            quantity=10.0,
            average_price=0.5,  # notional 5
            status="open",
            opened_at=datetime.now(timezone.utc),
        ))
    session_with_pnl.commit()
    engine = PaperTradingEngine(session_with_pnl)
    snap = engine._decision_capital_snapshot()
    # 2 × 10 × 0.5 = 10.0 notional
    assert snap["open_notional_usd"] == pytest.approx(10.0)
    assert snap["capital_available"] == pytest.approx(snap["effective_capital"] - 10.0, rel=0.001)


def test_snapshot_freshly_recomputed_on_each_call(session_with_pnl):
    """Critical: calling snapshot AFTER adding more PnL returns NEW effective.

    This is the heart of the bug #70 fix : the engine MUST re-read fresh,
    not return cached __init__ value.
    """
    engine = PaperTradingEngine(session_with_pnl)
    snap_before = engine._decision_capital_snapshot()
    # Simulate a closing trade adding +$10,000 PnL
    session_with_pnl.add(PaperTrade(
        id="boost",
        market_id="0xboost",
        outcome="Yes",
        side="BUY",
        quantity=1.0,
        average_price=0.5,
        realized_pnl=10000.0,
        status="closed",
        opened_at=EFFECTIVE_BASELINE_T0 + timedelta(hours=20),
        closed_at=EFFECTIVE_BASELINE_T0 + timedelta(hours=21),
    ))
    session_with_pnl.commit()
    snap_after = engine._decision_capital_snapshot()
    assert snap_after["effective_capital"] == pytest.approx(snap_before["effective_capital"] + 10000.0, rel=0.001)
    # Tier may have jumped if we crossed a threshold
    # At $77K still HUGE (< $128K threshold)
    assert snap_after["tier_name"] == "HUGE"


def test_small_capital_regression_nano_100(session_with_pnl):
    """Regression : à $100 capital, NANO résolu, max_pos=40, expo 75%."""
    # Wipe state to set paper_capital=100
    state = session_with_pnl.get(BotState, 1)
    state.paper_capital = 100.0
    # Wipe the PnL trades to keep small effective
    for t in session_with_pnl.exec(select(PaperTrade)).all():
        session_with_pnl.delete(t)
    session_with_pnl.commit()
    engine = PaperTradingEngine(session_with_pnl)
    snap = engine._decision_capital_snapshot()
    assert snap["paper_base"] == 100.0
    # No PnL => effective == base
    assert snap["effective_capital"] == pytest.approx(100.0)
    assert snap["tier_name"] == "NANO"
    assert snap["max_positions_cap"] == 40
    assert snap["max_total_exposure_pct"] == pytest.approx(0.75)


def test_exposure_helpers_use_fresh_capital_via_self_paper_capital(session_with_pnl):
    """current_exposure() and friends read self.paper_capital. After P0-A,
    `maybe_auto_trade` refreshes self.paper_capital BEFORE calling these,
    so the ratio uses fresh effective.

    Direct test : if we manually update self.paper_capital, current_exposure
    reflects the new ratio.
    """
    # Add 1 OPEN position with $1000 notional
    session_with_pnl.add(PaperTrade(
        id="op1",
        market_id="0xm1",
        outcome="Yes",
        side="BUY",
        quantity=1000.0,
        average_price=1.0,
        status="open",
        opened_at=datetime.now(timezone.utc),
    ))
    session_with_pnl.commit()

    engine = PaperTradingEngine(session_with_pnl)
    # Force-set paper_capital to base
    engine.paper_capital = 10000.0
    assert engine.current_exposure() == pytest.approx(0.10)  # 1000/10000 = 10%

    # Now refresh (= what maybe_auto_trade does) - effective ~$67K
    engine.paper_capital = engine._effective_paper_capital()
    # Ratio should now be ~1000/67300 ≈ 0.0149
    assert engine.current_exposure() < 0.02
    assert engine.current_exposure() == pytest.approx(1000.0 / 67300.0, rel=0.01)


def test_snapshot_logged_format_is_serializable(session_with_pnl):
    """Snapshot dict must be JSON-serializable for NoTradeDecision.details."""
    import json
    engine = PaperTradingEngine(session_with_pnl)
    snap = engine._decision_capital_snapshot()
    # Must serialize without error
    serialized = json.dumps(snap)
    assert "effective_capital" in serialized
    assert "tier_name" in serialized


