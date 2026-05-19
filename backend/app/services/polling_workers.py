"""P0-B (2026-05-18) — parallel async polling workers with priority queue.

Goal: replace the single-worker sequential ``run_loop`` with N async workers
consuming from a lane-aware priority queue, sharing a global TokenBucket.

Why: baseline 2026-05-18 19:52-20:54 (62min, 3647 records, observer JSONL)
shows source→seen p50=147s, p90=320s, with HOT and WARM near-identical lag.
Single-worker + min(next_due) + close-loop blocking work in the same loop
prevents HOT lane from delivering its theoretical priority.

Design rules (operator):
- Workers do ONLY: get → rate-limit → fetch → process → update_state → requeue.
- Close-loops and DB counter updates live in separate background tasks.
- Shared TokenBucket (single instance) enforces 18 calls/s globally.
- Priority HOT > WARM > COLD, with anti-starvation: WARM forced if wait
  > 2 × WARM_INTERVAL, COLD forced if wait > 2 × COLD_INTERVAL.
- Feature flag POLLING_WORKERS_ENABLED gates activation. Default false.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# --- Config ------------------------------------------------------------------

DEFAULT_WORKERS = 8
DEFAULT_HOT_INTERVAL_S = 10.0
DEFAULT_WARM_INTERVAL_S = 45.0
DEFAULT_COLD_INTERVAL_S = 120.0
# Anti-starvation thresholds: if a wallet has been waiting > N × its lane
# interval past due, escalate its priority so HOT cannot starve it.
ANTI_STARVE_MULTIPLIER = 2.0

# Priority weights — lower number = higher priority.
LANE_PRIORITY: dict[str, int] = {"HOT": 0, "WARM": 1, "COLD": 2}


def is_enabled() -> bool:
    """Feature flag — read on demand so flip + restart toggles in <30s."""
    try:
        return os.environ.get("POLLING_WORKERS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def workers_count() -> int:
    """Read worker count from env, fallback to DEFAULT_WORKERS."""
    try:
        n = int(os.environ.get("POLLING_WORKERS_COUNT", str(DEFAULT_WORKERS)))
        return max(1, min(32, n))
    except Exception:
        return DEFAULT_WORKERS


# --- Lane priority queue -----------------------------------------------------

@dataclass(order=True)
class _ScheduleItem:
    """Internal heapq entry. Ordered by (effective_priority, next_due, addr)."""
    effective_priority: int
    next_due: float
    addr: str = field(compare=False)
    lane: str = field(compare=False, default="WARM")


class LanePriorityQueue:
    """Asyncio-safe priority queue with lane-based scheduling + anti-starvation.

    Heap ordered by (effective_priority, next_due) :
    - HOT default priority = 0
    - WARM default priority = 1, escalated to 0 if wait > 2 × WARM_INTERVAL
    - COLD default priority = 2, escalated to 0 if wait > 2 × COLD_INTERVAL

    Escalation prevents HOT lane from starving WARM/COLD when many HOT wallets
    are continuously due. The escalation check happens at insertion + at pop:
    if the popped item is no longer the cheapest after escalation we re-heap.
    """

    def __init__(self) -> None:
        self._heap: list[_ScheduleItem] = []
        self._cond = asyncio.Condition()
        self._stopped = False
        # Track lane intervals for anti-starvation computation
        self._lane_interval: dict[str, float] = {
            "HOT": DEFAULT_HOT_INTERVAL_S,
            "WARM": DEFAULT_WARM_INTERVAL_S,
            "COLD": DEFAULT_COLD_INTERVAL_S,
        }

    def set_intervals(self, hot: float, warm: float, cold: float) -> None:
        self._lane_interval = {"HOT": hot, "WARM": warm, "COLD": cold}

    def _effective_priority(self, lane: str, next_due: float, now: float) -> int:
        """HOT always 0. WARM/COLD escalate to 0 if wait > 2×interval."""
        base = LANE_PRIORITY.get(lane, 1)
        if base == 0:
            return 0
        overdue = now - next_due
        if overdue > self._lane_interval.get(lane, DEFAULT_WARM_INTERVAL_S) * ANTI_STARVE_MULTIPLIER:
            return 0  # escalated
        return base

    async def put(self, addr: str, lane: str, next_due: float) -> None:
        async with self._cond:
            now = asyncio.get_event_loop().time()
            prio = self._effective_priority(lane, next_due, now)
            heapq.heappush(self._heap, _ScheduleItem(prio, next_due, addr, lane))
            self._cond.notify()

    async def get_when_due(self) -> tuple[str, str]:
        """Block until next item is due. Returns (addr, lane)."""
        while True:
            async with self._cond:
                if self._stopped:
                    raise asyncio.CancelledError("queue stopped")
                while not self._heap and not self._stopped:
                    await self._cond.wait()
                if self._stopped:
                    raise asyncio.CancelledError("queue stopped")
                # Re-score top items for escalation (cheap: only top)
                now = asyncio.get_event_loop().time()
                # Rescore top: pop, recompute, push back if changed.
                top = self._heap[0]
                cur_prio = self._effective_priority(top.lane, top.next_due, now)
                if cur_prio != top.effective_priority:
                    heapq.heappop(self._heap)
                    top.effective_priority = cur_prio
                    heapq.heappush(self._heap, top)
                item = self._heap[0]
                wait = max(0.0, item.next_due - now)
                if wait <= 0:
                    heapq.heappop(self._heap)
                    return item.addr, item.lane
            # Wait outside the lock so other workers can still put/notify.
            try:
                await asyncio.wait_for(self._notify_event(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def _notify_event(self) -> None:
        """Helper: block on notify."""
        async with self._cond:
            await self._cond.wait()

    async def stop(self) -> None:
        async with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def size(self) -> int:
        return len(self._heap)


# --- Worker pool -------------------------------------------------------------

@dataclass
class WorkerPool:
    """N async workers consuming a LanePriorityQueue. Each worker :
        addr, lane = await queue.get_when_due()
        async with rate_limiter:
            await poll_func(addr)
        next_due = now + interval_func(lane)
        await queue.put(addr, lane, next_due)
    """
    queue: LanePriorityQueue
    rate_limiter: Any  # TokenBucket with .acquire() async
    poll_func: Callable[[str], Awaitable[Any]]
    interval_func: Callable[[str], float]
    n_workers: int = DEFAULT_WORKERS
    stats: dict[str, Any] = field(default_factory=lambda: {"polls_done": 0, "errors": 0, "by_lane": {}})
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def _worker(self, worker_id: int) -> None:
        logger.info("polling_worker %d started", worker_id)
        try:
            while not self._stop_event.is_set():
                try:
                    addr, lane = await self.queue.get_when_due()
                except asyncio.CancelledError:
                    break
                try:
                    # P0-D 2026-05-19 fix double-rate-limiter : rate cap
                    # enforced inside fetch_recent_activity. See
                    # polling_workers_wrr._worker for full rationale.
                    await self.poll_func(addr)
                    self.stats["polls_done"] += 1
                    self.stats["by_lane"].setdefault(lane, 0)
                    self.stats["by_lane"][lane] += 1
                except Exception as exc:
                    self.stats["errors"] += 1
                    logger.warning(
                        "polling_worker err worker=%d wallet=%s lane=%s err=%r",
                        worker_id, addr, lane, exc,
                    )
                finally:
                    # Always requeue (so wallet keeps being polled even after error)
                    try:
                        interval = self.interval_func(lane)
                        next_due = asyncio.get_event_loop().time() + interval
                        await self.queue.put(addr, lane, next_due)
                    except Exception as exc:
                        logger.warning(
                            "polling_worker requeue err worker=%d wallet=%s err=%r",
                            worker_id, addr, exc,
                        )
        finally:
            logger.info("polling_worker %d stopped", worker_id)

    async def start(self) -> None:
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._worker(i), name=f"polling_worker_{i}")
            for i in range(self.n_workers)
        ]
        logger.info("WorkerPool started with %d workers", self.n_workers)

    async def stop(self) -> None:
        self._stop_event.set()
        await self.queue.stop()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("WorkerPool stopped. Final stats: %s", self.stats)
