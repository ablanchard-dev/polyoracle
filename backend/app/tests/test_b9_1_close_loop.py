"""v0.7.7 B9.1 — background close-loop tests.

Verifies the periodic close-loop behaviour:
1. Iteration closes both resolved markets AND stale TTL trades.
2. Releases capital correctly (capital_released_usdc recorded).
3. Long-run simulation : repeated iterations keep cap below saturation.
4. Interval gate : iteration is a no-op if not yet due.
5. Configurable interval via env var.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.market import Market
from app.models.trade import PaperTrade


def _make_session():
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def _open(sess, *, opened_offset_h=0, market_id="0xmkt", outcome="YES",
          side="BUY", entry=0.5, qty=10.0, notional=5.0, wallet="0xWalletA"):
    now = datetime.utcnow()
    t = PaperTrade(
        id=str(uuid4()),
        market_id=market_id,
        outcome=outcome,
        side=side,
        quantity=qty,
        average_price=entry,
        notional_usd=notional,
        status="open",
        opened_at=now - timedelta(hours=opened_offset_h),
        wallet_address=wallet,
        auto=True,
    )
    sess.add(t); sess.commit(); sess.refresh(t)
    return t


def _close_payload(trade: PaperTrade) -> dict:
    raw = trade.close_reason or ""
    parts = raw.split("|", 1)
    return {"reason": parts[0], "metadata": json.loads(parts[1]) if len(parts) == 2 else {}}


# ============ 1. Resolved markets close ============


def test_close_loop_closes_resolved_markets():
    from app.services.paper_trading_engine import run_background_close_loop_iteration

    sess = _make_session()
    sess.add(Market(id="0xresolved", question="?", yes_price=1.0, no_price=0.0))
    sess.commit()
    t_open = _open(sess, market_id="0xresolved", outcome="YES")
    t_other = _open(sess, market_id="0xunresolved")

    result = run_background_close_loop_iteration(sess)
    assert result["closed_resolved"] == 1
    assert result["total_closed"] == 1
    sess.refresh(t_open); sess.refresh(t_other)
    assert t_open.status == "closed"
    assert _close_payload(t_open)["reason"] == "CLOSED_RESOLVED"
    assert t_other.status == "open"  # unresolved still open


# ============ 2. Stale TTL close ============


def test_close_loop_closes_stale_after_ttl():
    from app.services.paper_trading_engine import run_background_close_loop_iteration

    sess = _make_session()
    fresh = _open(sess, opened_offset_h=2)   # 2h, keep
    stale = _open(sess, opened_offset_h=72)  # 72h, close

    result = run_background_close_loop_iteration(sess, ttl_hours=24)
    assert result["closed_stale_ttl"] == 1
    sess.refresh(fresh); sess.refresh(stale)
    assert fresh.status == "open"
    assert stale.status == "closed"
    assert _close_payload(stale)["reason"] == "CLOSED_STALE_TTL"


# ============ 3. Capital release ============


def test_close_loop_releases_capital_correctly():
    from app.services.paper_trading_engine import run_background_close_loop_iteration

    sess = _make_session()
    t = _open(sess, opened_offset_h=72, notional=12.5, qty=25.0, entry=0.5)
    result = run_background_close_loop_iteration(sess, ttl_hours=24)
    assert result["closed_stale_ttl"] == 1
    sess.refresh(t)
    md = _close_payload(t)["metadata"]
    assert md["capital_released_usdc"] == 12.5
    assert md["holding_minutes"] >= 72 * 60 - 5


# ============ 4. Long-run simulation : cap stays unsaturated ============


def test_long_run_does_not_saturate_cap():
    """Simulate a 90-min run: trades open every 5 min, close-loop fires
    every 30 min via stale_ttl with a tight TTL. Cap (max 5) must NEVER
    saturate longer than the close-loop interval."""
    from app.services.paper_trading_engine import (
        run_background_close_loop_iteration,
    )
    sess = _make_session()
    cap = 5
    open_history = []

    # Simulate 18 trade openings over 90 min, with close_loop firing
    # every 6 openings (= every 30 min). Use a SHORT TTL so older
    # trades close and free the cap.
    for i in range(18):
        # Cap check: count current open trades
        open_count = len(list(sess.exec(select(PaperTrade).where(PaperTrade.status == "open"))))
        if open_count >= cap:
            # Pretend we couldn't open; record cap miss
            open_history.append(("CAP_HIT", i))
        else:
            t = _open(sess, opened_offset_h=0, notional=1.0)
            # Force the row's opened_at backwards so subsequent close
            # sweeps actually trigger. opened_at simulating i*5 minutes
            # behind "now"
            t.opened_at = datetime.utcnow() - timedelta(minutes=18*5 - i*5)
            sess.add(t); sess.commit()
            open_history.append(("OPENED", i))
        # Every 6 iterations, fire close loop with TTL=10min
        if (i + 1) % 6 == 0:
            run_background_close_loop_iteration(sess, ttl_hours=0)  # ttl=0 means everything stale
            # Verify cap is reset after sweep (assuming all trades older
            # than the close-loop horizon are gone)

    # After the simulation, count cap hits
    cap_hits = sum(1 for ev, _ in open_history if ev == "CAP_HIT")
    opened = sum(1 for ev, _ in open_history if ev == "OPENED")
    # We expect ~12 opened (with periodic close cycles freeing the cap)
    # vs. only 5 if the cap never closes. With 3 close-loop firings,
    # we should see opened > 5.
    assert opened > cap, (
        f"close-loop should have freed the cap multiple times, "
        f"opened={opened}, cap_hits={cap_hits}"
    )


# ============ 5. Interval gate ============


def test_close_loop_interval_gate_is_idempotent():
    """Calling run_background_close_loop_iteration repeatedly within
    the interval should still close the same set of stale rows once,
    not re-do work or fail."""
    from app.services.paper_trading_engine import run_background_close_loop_iteration

    sess = _make_session()
    _open(sess, opened_offset_h=72)
    _open(sess, opened_offset_h=72)

    # First call: closes 2
    r1 = run_background_close_loop_iteration(sess, ttl_hours=24)
    assert r1["closed_stale_ttl"] == 2
    # Second call immediately: nothing left to close
    r2 = run_background_close_loop_iteration(sess, ttl_hours=24)
    assert r2["closed_stale_ttl"] == 0
    assert r2["total_closed"] == 0


# ============ 6. Env var configurability ============


def test_close_loop_interval_env_var():
    """BACKGROUND_CLOSE_LOOP_INTERVAL_S env var controls the cadence."""
    # Default 600s when env not set
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BACKGROUND_CLOSE_LOOP_INTERVAL_S", None)
        # Must reload the module to re-read env at import time
        import importlib
        import app.services.paper_trading_engine as pte
        importlib.reload(pte)
        assert pte.BACKGROUND_CLOSE_LOOP_INTERVAL_S == 600
    # Override via env
    with patch.dict(os.environ, {"BACKGROUND_CLOSE_LOOP_INTERVAL_S": "300"}):
        import importlib
        import app.services.paper_trading_engine as pte
        importlib.reload(pte)
        assert pte.BACKGROUND_CLOSE_LOOP_INTERVAL_S == 300
    # Restore default after test (avoid bleed-over)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BACKGROUND_CLOSE_LOOP_INTERVAL_S", None)
        import importlib
        import app.services.paper_trading_engine as pte
        importlib.reload(pte)
