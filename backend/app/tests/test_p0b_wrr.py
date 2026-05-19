"""P0-B v2 (2026-05-18) tests — WRR scheduler corrige le bug WARM/COLD starvation.

Operator test brief:
1. WARM > 0 polls AND COLD > 0 polls sur 5min simulated avec HOT always-head
2. Rate cap respecté
3. Scheduler stats expose served_by_lane
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter

import pytest

from app.services.polling_workers_wrr import (
    WrrScheduler, WrrWorkerPool, is_enabled, workers_count, _weights,
)
from app.services.wallet_polling_engine import TokenBucket


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("POLLING_WORKERS_ENABLED", raising=False)
    assert is_enabled() is False


def test_weights_defaults(monkeypatch):
    monkeypatch.delenv("POLLING_WRR_WEIGHT_HOT", raising=False)
    monkeypatch.delenv("POLLING_WRR_WEIGHT_WARM", raising=False)
    monkeypatch.delenv("POLLING_WRR_WEIGHT_COLD", raising=False)
    h, w, c = _weights()
    assert h == 47 and w == 47 and c == 6


def test_weights_env_override(monkeypatch):
    monkeypatch.setenv("POLLING_WRR_WEIGHT_HOT", "50")
    monkeypatch.setenv("POLLING_WRR_WEIGHT_WARM", "30")
    monkeypatch.setenv("POLLING_WRR_WEIGHT_COLD", "20")
    h, w, c = _weights()
    assert (h, w, c) == (50, 30, 20)


@pytest.mark.asyncio
async def test_wrr_serves_all_lanes_even_with_hot_dominant():
    """KEY TEST : reproduces the prod bug. HOT items continuously requeue.
    With WRR, WARM and COLD MUST still get polls > 0."""
    sched = WrrScheduler(weight_hot=47, weight_warm=47, weight_cold=6)
    bucket = TokenBucket(rate_per_sec=200, capacity=200)
    served = Counter()

    async def fake_poll(addr: str) -> None:
        served[addr] += 1

    pool = WrrWorkerPool(
        scheduler=sched, rate_limiter=bucket, poll_func=fake_poll,
        interval_func=lambda lane: {"HOT": 0.1, "WARM": 0.3, "COLD": 0.5}[lane],
        n_workers=4,
    )
    now = asyncio.get_event_loop().time()
    # Seed HOT-heavy cohort similar to prod (80 HOT, 382 WARM, 283 COLD)
    for i in range(80):
        await sched.put(f"0xH{i:03d}", "HOT", now)
    for i in range(382):
        await sched.put(f"0xW{i:03d}", "WARM", now)
    for i in range(283):
        await sched.put(f"0xC{i:03d}", "COLD", now)

    await pool.start()
    await asyncio.sleep(2.0)  # simulate 2s of polling
    await pool.stop()

    by_lane = pool.stats["by_lane"]
    print(f"WRR stats by lane: {by_lane}")
    assert by_lane["HOT"] > 0, "HOT served zero (impossible)"
    assert by_lane["WARM"] > 0, f"WARM STARVED — WRR bug: {by_lane}"
    assert by_lane["COLD"] > 0, f"COLD STARVED — WRR bug: {by_lane}"


@pytest.mark.asyncio
async def test_wrr_rate_cap_respected_via_poll_func():
    """P0-D fix : rate cap enforced INSIDE poll_func (i.e. fetch_recent_activity
    in prod), not in WrrWorkerPool dispatcher. This test simulates that by
    having poll_func itself acquire from the bucket."""
    sched = WrrScheduler(47, 47, 6)
    bucket = TokenBucket(rate_per_sec=18, capacity=18)
    times = []

    async def fake_poll(addr: str) -> None:
        # Simulate fetch_recent_activity: acquire token, then make API call.
        await bucket.acquire()
        times.append(time.perf_counter())

    pool = WrrWorkerPool(
        scheduler=sched, rate_limiter=bucket, poll_func=fake_poll,
        interval_func=lambda lane: 0.05,
        n_workers=8,
    )
    now = asyncio.get_event_loop().time()
    for i in range(50):
        await sched.put(f"0xH{i}", "HOT", now)
    for i in range(50):
        await sched.put(f"0xW{i}", "WARM", now)

    await pool.start()
    await asyncio.sleep(2.0)
    await pool.stop()

    n = len(times)
    # Budget: 18 polls/s × 2s + initial burst 18 ≤ 54
    assert n <= 18 * 2 + 18, f"rate cap violated: {n} polls in 2s"
    assert n >= 18, f"too few polls: {n}"


@pytest.mark.asyncio
async def test_wrr_dispatcher_no_token_consumption():
    """P0-D regression test : WrrWorkerPool dispatcher must NOT acquire
    tokens itself. If it did, 1 poll would cost 2 tokens (one in dispatcher,
    one in fetch_recent_activity), halving effective cadence.

    With dispatcher NOT acquiring + poll_func that doesn't acquire either,
    we should see ~unlimited polls/s (limited only by asyncio overhead)."""
    sched = WrrScheduler(47, 47, 6)
    bucket = TokenBucket(rate_per_sec=18, capacity=18)
    count = {"n": 0}

    async def fake_poll_no_acquire(addr: str) -> None:
        # NO bucket.acquire() — proves dispatcher doesn't gate
        count["n"] += 1

    pool = WrrWorkerPool(
        scheduler=sched, rate_limiter=bucket, poll_func=fake_poll_no_acquire,
        interval_func=lambda lane: 0.001,  # ultra-fast requeue
        n_workers=4,
    )
    now = asyncio.get_event_loop().time()
    for i in range(20):
        await sched.put(f"0xH{i}", "HOT", now)
    await pool.start()
    await asyncio.sleep(0.3)
    await pool.stop()
    # Without rate limit, should easily exceed 18 × 0.3 = 5.4 (the old cap)
    assert count["n"] > 50, f"dispatcher unexpectedly throttling: {count['n']} polls in 0.3s"


@pytest.mark.asyncio
async def test_wrr_proportional_distribution():
    """Distribution polls approximate weighted share over long run.
    Post-P0-D fix: poll_func acquires from bucket (simulates fetch_recent_activity
    rate gate). Without this, workers cycle so fast deficit calculation can't
    converge to weighted distribution."""
    sched = WrrScheduler(50, 30, 20)
    bucket = TokenBucket(rate_per_sec=200, capacity=200)
    served_per_lane = Counter()

    async def fake_poll(addr: str) -> None:
        # Simulate real fetch_recent_activity : rate-limited API call.
        await bucket.acquire()

    pool = WrrWorkerPool(
        scheduler=sched, rate_limiter=bucket, poll_func=fake_poll,
        interval_func=lambda lane: 0.01,  # very fast requeue
        n_workers=4,
    )
    now = asyncio.get_event_loop().time()
    for i in range(20):
        await sched.put(f"0xH{i}", "HOT", now)
        await sched.put(f"0xW{i}", "WARM", now)
        await sched.put(f"0xC{i}", "COLD", now)

    await pool.start()
    await asyncio.sleep(1.5)
    await pool.stop()

    by_lane = pool.stats["by_lane"]
    total = sum(by_lane.values())
    if total < 30:
        pytest.skip(f"too few polls to verify distribution: {total}")
    hot_share = by_lane["HOT"] / total
    warm_share = by_lane["WARM"] / total
    cold_share = by_lane["COLD"] / total
    # Allow ±15pt slack for stochastic deviations on short runs
    assert 0.35 <= hot_share <= 0.65, f"HOT share {hot_share:.2f} off target 0.50"
    assert 0.15 <= warm_share <= 0.45, f"WARM share {warm_share:.2f} off target 0.30"
    assert 0.05 <= cold_share <= 0.35, f"COLD share {cold_share:.2f} off target 0.20"


@pytest.mark.asyncio
async def test_wrr_handles_poll_exception():
    sched = WrrScheduler(47, 47, 6)
    bucket = TokenBucket(rate_per_sec=100, capacity=100)
    n_calls = {"v": 0}

    async def faulty(addr: str) -> None:
        n_calls["v"] += 1
        if n_calls["v"] < 3:
            raise RuntimeError("simulated")

    pool = WrrWorkerPool(
        scheduler=sched, rate_limiter=bucket, poll_func=faulty,
        interval_func=lambda lane: 0.01, n_workers=1,
    )
    await sched.put("0xH1", "HOT", asyncio.get_event_loop().time())
    await pool.start()
    await asyncio.sleep(0.5)
    await pool.stop()
    assert n_calls["v"] >= 3
    assert pool.stats["errors"] >= 2


@pytest.mark.asyncio
async def test_wrr_size_and_stats_apis():
    sched = WrrScheduler(47, 47, 6)
    await sched.put("0xH1", "HOT", asyncio.get_event_loop().time() + 10)
    await sched.put("0xW1", "WARM", asyncio.get_event_loop().time() + 10)
    await sched.put("0xC1", "COLD", asyncio.get_event_loop().time() + 10)
    assert sched.size() == 3
    assert sched.size_by_lane() == {"HOT": 1, "WARM": 1, "COLD": 1}
    assert sched.served_by_lane() == {"HOT": 0, "WARM": 0, "COLD": 0}
