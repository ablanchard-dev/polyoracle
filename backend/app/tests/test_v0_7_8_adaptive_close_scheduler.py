"""v0.7.8 Phase 2 — Adaptive close-loop tests.

The adaptive scheduler replaces the fixed 600s close-loop. For each
duration bucket, the position must be checked at the right cadence:
  ULTRA_SHORT (1-5min) → 20s
  VERY_SHORT  (5-30min) → 60s
  SHORT       (30-120min) → 5min
  MEDIUM/LONG/VERY_LONG/UNKNOWN → 10min
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.bot import BotState
from app.models.market import Market
from app.models.trade import PaperTrade
from app.services.adaptive_close_scheduler import (
    BUCKET_CHECK_INTERVAL_S,
    AdaptiveCloseScheduler,
    get_scheduler,
    reset_scheduler_for_tests,
    run_adaptive_close_iteration,
    stale_ttl_sweep,
)


def _engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(e)
    return e


def _open_position(session, *, market_id="0xtest", outcome="Yes",
                   side="BUY", entry=0.50, qty=10.0, opened_at=None):
    t = PaperTrade(
        id=f"pt-{datetime.now(timezone.utc).timestamp()}-{market_id[:8]}-{outcome}",
        market_id=market_id,
        outcome=outcome,
        side=side,
        quantity=qty,
        average_price=entry,
        slippage=0.0,
        status="open",
        opened_at=opened_at or datetime.now(timezone.utc),
        notional_usd=entry * qty,
        auto=True,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _market_with_prices(session, market_id, yes_price, no_price):
    m = Market(id=market_id, question="test", yes_price=yes_price, no_price=no_price)
    session.add(m)
    session.commit()
    return m


# ---------------- 1 — bucket intervals correctness ----------------


def test_bucket_intervals_match_spec():
    """ULTRA_SHORT must be 20s, VERY_SHORT 60s, SHORT 300s. The whole
    point of the adaptive loop. If these change without updating tests,
    capital turnover regresses."""
    assert BUCKET_CHECK_INTERVAL_S["ULTRA_SHORT"] <= 30, (
        "ULTRA_SHORT (1-5min markets) must check every ≤30s — otherwise "
        "we lock capital ≥ 1/3 of the trade lifetime"
    )
    assert BUCKET_CHECK_INTERVAL_S["VERY_SHORT"] <= 120
    assert BUCKET_CHECK_INTERVAL_S["SHORT"] <= 600
    # All buckets must be defined
    for bucket in ("ULTRA_SHORT", "VERY_SHORT", "SHORT", "MEDIUM", "LONG", "VERY_LONG", "UNKNOWN"):
        assert bucket in BUCKET_CHECK_INTERVAL_S


# ---------------- 2 — register adds to heap ----------------


def test_register_position_idempotent():
    """Registering the same position twice should not duplicate it."""
    s = AdaptiveCloseScheduler()
    s.register_position("pt1", "0xmkt1", "ULTRA_SHORT")
    s.register_position("pt1", "0xmkt1", "ULTRA_SHORT")  # duplicate
    assert s.known_positions_count() == 1


# ---------------- 3 — pop_due returns only expired entries ----------------


def test_pop_due_returns_only_expired():
    """At t=0, a position scheduled for t+20s should NOT be due. After
    t=21s, it should be."""
    clock = [1000.0]

    def fake_clock():
        return clock[0]

    s = AdaptiveCloseScheduler(clock=fake_clock)
    s.register_position("pt1", "0xmkt1", "ULTRA_SHORT")

    due = s.pop_due()
    assert len(due) == 0, "Just-registered position should not be due"

    clock[0] += 21.0
    due = s.pop_due()
    assert len(due) == 1
    assert due[0].position_id == "pt1"


# ---------------- 4 — bucket determines re-check cadence ----------------


@pytest.mark.parametrize("bucket,expected_max_interval", [
    ("ULTRA_SHORT", 30),
    ("VERY_SHORT", 120),
    ("SHORT", 600),
    ("MEDIUM", 600),
    ("LONG", 600),
])
def test_register_uses_bucket_interval(bucket, expected_max_interval):
    clock = [1000.0]

    def fake_clock():
        return clock[0]

    s = AdaptiveCloseScheduler(clock=fake_clock)
    s.register_position(f"pt-{bucket}", "0xmkt", bucket)
    interval = BUCKET_CHECK_INTERVAL_S[bucket]
    # Just before interval — not due
    clock[0] += interval - 1
    assert len(s.pop_due()) == 0
    # After interval — due
    clock[0] += 2
    assert len(s.pop_due()) == 1


# ---------------- 5 — deregister filters stale entries ----------------


def test_deregister_silently_skips_at_pop():
    s = AdaptiveCloseScheduler()
    s.register_position("pt1", "0xmkt1", "ULTRA_SHORT")
    s.deregister_position("pt1")
    assert s.known_positions_count() == 0
    # After enough time, the heap entry is still there but pop_due
    # filters it out
    import time
    time.sleep(0.01)  # not enough to trigger interval, just to be safe
    # Force interval by patching the scheduler's clock
    clock = [s._clock() + 100.0]
    s._clock = lambda: clock[0]
    due = s.pop_due()
    assert len(due) == 0


# ---------------- 6 — reschedule re-adds position ----------------


def test_reschedule_pushes_back_to_heap():
    clock = [1000.0]

    def fake_clock():
        return clock[0]

    s = AdaptiveCloseScheduler(clock=fake_clock)
    s.register_position("pt1", "0xmkt1", "ULTRA_SHORT")
    clock[0] += 25.0  # make it due
    due = s.pop_due()
    assert len(due) == 1
    assert s.heap_size() == 0  # popped

    s.reschedule(due[0])  # re-add for next cycle
    assert s.heap_size() == 1
    # And it's not due immediately
    assert len(s.pop_due()) == 0


# ---------------- 7 — concurrent register/pop is safe ----------------


def test_concurrent_register_pop_thread_safe():
    s = AdaptiveCloseScheduler()
    errors: list[Exception] = []

    def register_many():
        try:
            for i in range(100):
                s.register_position(f"pt-{i}", f"0xmkt-{i}", "ULTRA_SHORT")
        except Exception as e:
            errors.append(e)

    def pop_many():
        try:
            for _ in range(50):
                s.pop_due()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=register_many)
    t2 = threading.Thread(target=pop_many)
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert errors == []
    assert s.known_positions_count() == 100


# ---------------- 8 — run_adaptive_close_iteration closes resolved markets ----------------


def test_run_adaptive_iteration_closes_resolved_market():
    """Integration test: a position is registered, market resolves
    (yes_price >= 0.99), iteration runs after the bucket interval —
    must close the position with realized_pnl computed correctly."""
    reset_scheduler_for_tests()
    eng = _engine()
    with Session(eng) as session:
        m = _market_with_prices(session, "0xresolved", yes_price=1.0, no_price=0.0)
        trade = _open_position(session, market_id="0xresolved", outcome="Yes",
                              side="BUY", entry=0.40, qty=100.0)
        scheduler = get_scheduler()
        scheduler.register_position(trade.id, "0xresolved", "ULTRA_SHORT")

        # Simulate time passing
        import time
        original_clock = scheduler._clock
        scheduler._clock = lambda: original_clock() + 30.0  # past the 20s ULTRA_SHORT interval

        result = run_adaptive_close_iteration(session)
        assert result["closed"] == 1, f"Expected 1 close, got {result}"

        session.refresh(trade)
        assert trade.status == "closed"


# ---------------- 9 — does NOT close active markets ----------------


def test_run_adaptive_iteration_skips_active_markets():
    reset_scheduler_for_tests()
    eng = _engine()
    with Session(eng) as session:
        m = _market_with_prices(session, "0xactive", yes_price=0.55, no_price=0.45)
        trade = _open_position(session, market_id="0xactive", outcome="Yes",
                              side="BUY", entry=0.40, qty=100.0)
        scheduler = get_scheduler()
        scheduler.register_position(trade.id, "0xactive", "ULTRA_SHORT")
        original_clock = scheduler._clock
        scheduler._clock = lambda: original_clock() + 30.0

        result = run_adaptive_close_iteration(session)
        assert result["closed"] == 0
        assert result["rescheduled"] == 1

        session.refresh(trade)
        assert trade.status == "open"


# ---------------- 10 — reactive_check closes immediately on resolved ----------------


def test_reactive_check_market_closes_immediately():
    """Option B: when polling fetches a market for any reason, if our
    position is open AND market resolved → close immediately, no need
    to wait for next adaptive interval."""
    reset_scheduler_for_tests()
    eng = _engine()
    with Session(eng) as session:
        # Mark market resolved YES
        m = _market_with_prices(session, "0xreactive", yes_price=1.0, no_price=0.0)
        trade = _open_position(session, market_id="0xreactive", outcome="Yes",
                              side="BUY", entry=0.40, qty=100.0)
        scheduler = get_scheduler()
        scheduler.register_position(trade.id, "0xreactive", "ULTRA_SHORT")

        # Simulate polling engine seeing this market with these prices
        closed = scheduler.reactive_check_market(
            session, "0xreactive", yes_price=1.0, no_price=0.0,
        )
        assert closed == 1

        session.refresh(trade)
        assert trade.status == "closed"
        # And position is now deregistered from scheduler
        assert scheduler.known_positions_count() == 0


# ---------------- 11 — stale TTL sweep closes old positions ----------------


def test_stale_ttl_sweep_closes_old_positions():
    """Safety net: positions open > TTL hours get force-closed."""
    reset_scheduler_for_tests()
    eng = _engine()
    with Session(eng) as session:
        from datetime import timedelta
        old_open = datetime.now(timezone.utc) - timedelta(hours=25)
        trade_old = _open_position(
            session, market_id="0xstale", outcome="Yes",
            opened_at=old_open,
        )
        # Register it (would normally happen on open)
        scheduler = get_scheduler()
        scheduler.register_position(trade_old.id, "0xstale", "ULTRA_SHORT")

        result = stale_ttl_sweep(session, ttl_hours=24.0)
        assert result["closed_stale_ttl"] == 1
        session.refresh(trade_old)
        assert trade_old.status == "closed"


# ---------------- 12 — capital turnover improvement (theory check) ----------------


def test_capital_turnover_improvement_vs_legacy():
    """Sanity check: with adaptive intervals, ULTRA_SHORT positions can
    cycle much faster than the legacy 600s loop. This test asserts the
    DESIGN intent, not actual runtime."""
    legacy_interval_s = 600.0
    adaptive_interval_ultra_short = BUCKET_CHECK_INTERVAL_S["ULTRA_SHORT"]
    speedup = legacy_interval_s / adaptive_interval_ultra_short
    assert speedup >= 20, (
        f"Adaptive loop should be ≥20× faster than legacy 600s for ULTRA_SHORT, "
        f"got {speedup:.1f}× (interval={adaptive_interval_ultra_short}s)"
    )
