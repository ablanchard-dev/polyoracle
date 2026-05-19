"""P0-B (2026-05-18) tests — async parallel polling workers.

Covers:
1. Rate limit respect (N workers stay <= 18 calls/s)
2. HOT priority over WARM when both due
3. Anti-starvation: WARM/COLD escalate when wait > 2 × interval
4. PollingState concurrent update safe (threading.RLock)
5. Feature flag respect
6. Worker handles poll_func exception without dying
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.polling_workers import (
    LanePriorityQueue,
    WorkerPool,
    is_enabled,
    workers_count,
    DEFAULT_HOT_INTERVAL_S,
    DEFAULT_WARM_INTERVAL_S,
    DEFAULT_COLD_INTERVAL_S,
)
from app.services.wallet_polling_engine import PollingState, TokenBucket


# ---- 1. Feature flag --------------------------------------------------------

def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("POLLING_WORKERS_ENABLED", raising=False)
    assert is_enabled() is False


def test_flag_on_variants(monkeypatch):
    for v in ("true", "True", "1", "yes", "on"):
        monkeypatch.setenv("POLLING_WORKERS_ENABLED", v)
        assert is_enabled() is True
    for v in ("false", "0", "", "no"):
        monkeypatch.setenv("POLLING_WORKERS_ENABLED", v)
        assert is_enabled() is False


def test_workers_count_clamp(monkeypatch):
    monkeypatch.setenv("POLLING_WORKERS_COUNT", "8")
    assert workers_count() == 8
    monkeypatch.setenv("POLLING_WORKERS_COUNT", "999")
    assert workers_count() == 32  # clamped
    monkeypatch.setenv("POLLING_WORKERS_COUNT", "0")
    assert workers_count() == 1  # min 1
    monkeypatch.setenv("POLLING_WORKERS_COUNT", "garbage")
    from app.services.polling_workers import DEFAULT_WORKERS
    assert workers_count() == DEFAULT_WORKERS


# ---- 2. PollingState lock concurrent safety --------------------------------

def test_polling_state_concurrent_updates_no_loss(tmp_path):
    """Hammer PollingState.update from many threads. No update lost, JSON valid."""
    state_file = tmp_path / "state.json"
    state = PollingState(state_file=state_file)

    def worker(start_addr: int, count: int):
        for i in range(count):
            state.update(f"0xw{start_addr + i:04d}", 1_700_000_000 + i)

    threads = [threading.Thread(target=worker, args=(t * 100, 50)) for t in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    # 8 threads × 50 unique addrs = 400 unique wallets must all be present
    assert len(state.last_seen_ts) == 400
    # File on disk is valid JSON and has all entries
    import json
    on_disk = json.loads(state_file.read_text())
    assert len(on_disk) == 400


def test_polling_state_monotonic_only_updates(tmp_path):
    """update() must keep MAX timestamp, never go back."""
    state = PollingState(state_file=tmp_path / "s.json")
    state.update("0xa", 100)
    state.update("0xa", 200)
    state.update("0xa", 50)  # ← lower, ignored
    assert state.last_seen_ts["0xa"] == 200


# ---- 3. LanePriorityQueue HOT priority -------------------------------------

@pytest.mark.asyncio
async def test_hot_priority_over_warm_when_both_due():
    q = LanePriorityQueue()
    now = asyncio.get_event_loop().time()
    await q.put("0xwarm1", "WARM", now - 1)  # due now
    await q.put("0xhot1", "HOT", now - 1)    # due now
    await q.put("0xwarm2", "WARM", now - 1)
    await q.put("0xhot2", "HOT", now - 1)

    # HOT should pop first regardless of insertion order
    addr1, lane1 = await asyncio.wait_for(q.get_when_due(), timeout=1.0)
    addr2, lane2 = await asyncio.wait_for(q.get_when_due(), timeout=1.0)
    assert lane1 == "HOT"
    assert lane2 == "HOT"


@pytest.mark.asyncio
async def test_anti_starvation_warm_escalates_when_overdue():
    """WARM wallet waiting > 2 × WARM_INTERVAL should escalate to priority 0."""
    q = LanePriorityQueue()
    q.set_intervals(hot=10.0, warm=5.0, cold=10.0)  # warm 5s so escalation fast
    now = asyncio.get_event_loop().time()
    # WARM overdue by 12s = > 2×5s threshold → should escalate to HOT priority
    await q.put("0xwarm_overdue", "WARM", now - 12.0)
    await q.put("0xhot_just_due", "HOT", now - 0.1)

    addr, lane = await asyncio.wait_for(q.get_when_due(), timeout=1.0)
    # Both have effective priority 0 now; min(next_due) wins, which is warm_overdue
    assert addr == "0xwarm_overdue"


@pytest.mark.asyncio
async def test_warm_not_starved_in_long_run():
    """In a HOT-heavy stream, WARM still gets served eventually."""
    q = LanePriorityQueue()
    q.set_intervals(hot=1.0, warm=2.0, cold=5.0)
    now = asyncio.get_event_loop().time()
    # 5 HOT all due, 1 WARM overdue
    for i in range(5):
        await q.put(f"0xhot{i}", "HOT", now - 0.1)
    await q.put("0xwarm_overdue", "WARM", now - 5.0)  # >> 2×2 = 4

    served = []
    for _ in range(6):
        a, _ = await asyncio.wait_for(q.get_when_due(), timeout=1.0)
        served.append(a)
    assert "0xwarm_overdue" in served


# ---- 4. WorkerPool integration ----------------------------------------------

@pytest.mark.asyncio
async def test_worker_pool_rate_limit_respected():
    """P0-D fix : rate cap enforced INSIDE poll_func (i.e. fetch_recent_activity
    in prod), not in WorkerPool dispatcher. Test simulates that by having
    poll_func itself acquire from the bucket."""
    q = LanePriorityQueue()
    bucket = TokenBucket(rate_per_sec=18, capacity=18)
    call_times: list[float] = []

    async def fake_poll(addr: str) -> None:
        await bucket.acquire()  # Simulate fetch_recent_activity rate gate
        call_times.append(time.perf_counter())

    pool = WorkerPool(
        queue=q,
        rate_limiter=bucket,
        poll_func=fake_poll,
        interval_func=lambda lane: 0.05,  # quick requeue
        n_workers=8,
    )

    # Pre-fill queue with 100 HOT items all due
    now = asyncio.get_event_loop().time()
    for i in range(100):
        await q.put(f"0xh{i:03d}", "HOT", now - 1)

    await pool.start()
    await asyncio.sleep(2.0)  # 2-second observation window
    await pool.stop()

    # Total polls in 2s shouldn't exceed 18 × 2 + capacity headroom
    # Budget: 18 polls/s × 2s = 36, allow up to capacity (18) extra burst
    n = len(call_times)
    assert n <= 18 * 2 + 18, f"rate cap violated: {n} polls in 2s"
    # Sanity: at least 18 polls happened (not stalled)
    assert n >= 18, f"too few polls: {n}"


@pytest.mark.asyncio
async def test_worker_handles_poll_exception():
    """If poll_func raises, worker continues, item is requeued."""
    q = LanePriorityQueue()
    bucket = TokenBucket(rate_per_sec=100, capacity=100)
    call_count = {"n": 0}

    async def faulty_poll(addr: str) -> None:
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError("simulated")
        # 3rd+ call succeeds

    pool = WorkerPool(
        queue=q,
        rate_limiter=bucket,
        poll_func=faulty_poll,
        interval_func=lambda lane: 0.01,
        n_workers=1,
    )
    now = asyncio.get_event_loop().time()
    await q.put("0xfaulty", "HOT", now)
    await pool.start()
    await asyncio.sleep(0.5)
    await pool.stop()

    assert call_count["n"] >= 3
    assert pool.stats["errors"] >= 2


@pytest.mark.asyncio
async def test_worker_pool_stats_track_lane():
    q = LanePriorityQueue()
    # Short intervals so anti-starvation kicks in fast (WARM threshold = 2×0.3 = 0.6s)
    q.set_intervals(hot=0.5, warm=0.3, cold=0.5)
    bucket = TokenBucket(rate_per_sec=100, capacity=100)
    seen = []

    async def fake_poll(addr: str) -> None:
        seen.append(addr)

    pool = WorkerPool(
        queue=q,
        rate_limiter=bucket,
        poll_func=fake_poll,
        interval_func=lambda lane: 0.3,
        n_workers=2,
    )
    now = asyncio.get_event_loop().time()
    await q.put("0xh1", "HOT", now - 0.1)
    await q.put("0xw1", "WARM", now - 1.0)  # already past anti-starve threshold
    await q.put("0xh2", "HOT", now - 0.1)

    await pool.start()
    await asyncio.sleep(1.5)
    await pool.stop()

    assert pool.stats["polls_done"] >= 3, f"got {pool.stats['polls_done']}"
    assert pool.stats["by_lane"].get("HOT", 0) >= 2
    assert pool.stats["by_lane"].get("WARM", 0) >= 1, f"WARM starved: by_lane={pool.stats['by_lane']}"
