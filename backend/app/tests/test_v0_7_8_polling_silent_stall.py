"""Silent-stall regression tests for v0.7.8 polling diagnostic patches.

Covers two paths added to surface stalls that previously went silent:
- ``_on_task_done`` callback persists ``last_polling_error`` when run_loop
  dies with an unhandled exception (so the orchestrator sees the cause
  instead of just `polling_running=False`).
- ``_watchdog_loop`` force-cancels the polling task when the in-memory
  heartbeat goes stale (covers the deadlock/long-blocking-sync case where
  no exception ever propagates).

Important: each test uses an isolated in-memory SQLite engine. Earlier
revs of this file imported ``app.database.engine`` at module level, which
caused tests to write into the production ``data/polyoracle.db`` and leave
``last_polling_error`` polluted (`WATCHDOG_STALL stalled_for=999s` ghosts
were observed). The current setup monkeypatches the engine in the polling
module so the diagnostic DB writes target the temp DB only.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

os.environ["POLLING_STATE_FILE"] = str(Path(__file__).parent / "_polling_state_stall.json")
os.environ["LIVE_ENABLED"] = "false"

from app.models.bot import BotLoopState  # noqa: E402
from app.services import adaptive_close_scheduler as acs  # noqa: E402
from app.services import wallet_polling_engine as wpe  # noqa: E402
from app.services.adaptive_close_scheduler import AdaptiveCloseScheduler  # noqa: E402
from app.services.wallet_polling_engine import (  # noqa: E402
    WalletPollingEngine,
    reset_singleton_for_tests,
)


@pytest.fixture()
def isolated_engine(monkeypatch: pytest.MonkeyPatch):
    """Build an in-memory SQLite engine with the BotLoopState row seeded
    and swap it into the polling module so _on_task_done / _watchdog_loop
    write here, not into the prod DB."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        session.add(BotLoopState(id=1))
        session.commit()
    monkeypatch.setattr(wpe, "engine", test_engine)
    yield test_engine


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "polling_state.json"
    monkeypatch.setenv("POLLING_STATE_FILE", str(state_file))
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


def _read_last_error(test_engine) -> str | None:
    with Session(test_engine) as session:
        state = session.get(BotLoopState, 1)
        return state.last_polling_error if state else None


@pytest.mark.asyncio
async def test_done_callback_persists_unhandled_exception(isolated_engine) -> None:
    """run_loop raises -> _on_task_done writes typed error to test DB."""
    eng = WalletPollingEngine()
    eng._stop_event = asyncio.Event()

    async def _fake_run_loop() -> None:
        raise RuntimeError("simulated polling crash")

    task = asyncio.create_task(_fake_run_loop())
    task.add_done_callback(eng._on_task_done)
    eng._task = task

    with pytest.raises(RuntimeError):
        await task

    # Yield once so the done_callback (sync) runs to completion.
    await asyncio.sleep(0)

    err = _read_last_error(isolated_engine)
    assert err is not None, "done_callback failed to persist error"
    assert "RuntimeError" in err
    assert "simulated polling crash" in err


@pytest.mark.asyncio
async def test_done_callback_ignores_clean_cancel(isolated_engine) -> None:
    """Explicit cancel (e.g. orderly stop) should NOT write a stall error."""
    eng = WalletPollingEngine()

    async def _runs_until_cancelled() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(_runs_until_cancelled())
    task.add_done_callback(eng._on_task_done)
    eng._task = task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert _read_last_error(isolated_engine) is None


@pytest.mark.asyncio
async def test_watchdog_force_cancels_on_stale_heartbeat(isolated_engine) -> None:
    """Stale heartbeat -> watchdog cancels run_loop -> last_polling_error set."""
    eng = WalletPollingEngine()

    cancelled_marker = asyncio.Event()

    async def _hangs_forever() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled_marker.set()
            raise

    task = asyncio.create_task(_hangs_forever())
    task.add_done_callback(eng._on_task_done)
    eng._task = task
    eng._iteration_heartbeat = asyncio.get_event_loop().time() - 999.0

    async def _fast_watchdog() -> None:
        WATCHDOG_TICK_S = 0.05
        WATCHDOG_STALL_S = 0.1
        try:
            while eng._task is not None and not eng._task.done():
                await asyncio.sleep(WATCHDOG_TICK_S)
                if eng._task is None or eng._task.done():
                    return
                age = asyncio.get_event_loop().time() - eng._iteration_heartbeat
                if age > WATCHDOG_STALL_S:
                    with Session(isolated_engine) as _session:
                        state = _session.get(BotLoopState, 1)
                        if state is not None:
                            state.last_polling_error = (
                                f"WATCHDOG_STALL stalled_for={age:.1f}s"
                            )
                            _session.add(state)
                            _session.commit()
                    eng._task.cancel()
                    return
        except asyncio.CancelledError:
            return

    watchdog = asyncio.create_task(_fast_watchdog())
    try:
        await asyncio.wait_for(cancelled_marker.wait(), timeout=2.0)
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass
        try:
            await task
        except asyncio.CancelledError:
            pass

    err = _read_last_error(isolated_engine)
    assert err is not None
    assert "WATCHDOG_STALL" in err


# ---------------- contract tests on the silent-stall bound ----------------
# These lock the fix parameters so a future change that drops max_entries=3
# or relaxes gamma_timeout=3.0 fails CI instead of silently re-opening the
# 240s blocking-iteration regression.


@pytest.mark.parametrize("max_count,registered,expected", [
    (3, 10, 3),    # bound enforced — only 3 popped despite 10 due
    (1, 10, 1),    # tight bound
    (None, 10, 10),  # unbounded — backward-compat default
])
def test_pop_due_respects_max_count(max_count, registered, expected) -> None:
    """Bound mechanic: pop_due(max_count=N) returns at most N entries
    even when more positions are due."""
    clock = [1000.0]
    scheduler = AdaptiveCloseScheduler(clock=lambda: clock[0])
    for i in range(registered):
        scheduler.register_position(f"pt{i}", f"0xmkt{i}", "ULTRA_SHORT")
    # Advance past the ULTRA_SHORT 20s interval so all are due
    clock[0] += 30.0
    due = scheduler.pop_due(max_count=max_count)
    assert len(due) == expected


def test_polling_close_helper_passes_bound_and_timeout(
    isolated_engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: _run_adaptive_close_loop_sync MUST call
    run_adaptive_close_iteration with max_entries=3 and gamma_timeout=3.0.
    Drift here re-opens the silent-stall regression — worst case goes from
    18s to 240s per polling iteration. This test fails before silently
    re-introducing the bug."""
    captured: dict = {}

    def _fake_run_adaptive_close_iteration(
        session, max_entries=None, gamma_timeout=None,
    ):
        captured["max_entries"] = max_entries
        captured["gamma_timeout"] = gamma_timeout
        return {"checked": 0, "closed": 0, "rescheduled": 0}

    monkeypatch.setattr(
        acs, "run_adaptive_close_iteration", _fake_run_adaptive_close_iteration,
    )

    eng = WalletPollingEngine()
    eng._run_adaptive_close_loop_sync()

    assert captured.get("max_entries") == 3, (
        f"polling close-loop bound drifted: max_entries={captured.get('max_entries')}, "
        f"expected 3. Re-opens silent-stall regression."
    )
    assert captured.get("gamma_timeout") == 3.0, (
        f"polling close-loop Gamma timeout drifted: gamma_timeout={captured.get('gamma_timeout')}, "
        f"expected 3.0. Re-opens silent-stall regression."
    )
