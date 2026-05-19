"""P0-B v2 (2026-05-18) — Weighted Round Robin polling scheduler.

REMPLACE polling_workers.py (strict priority queue) qui starvait WARM/COLD.

Diagnostic confirmé sur prod 2026-05-18 22:55 UTC, 5 min monitoring:
- 2064 polls done in 5min
- ALL 2064 in HOT lane
- 0 WARM, 0 COLD polls
- → 665 wallets (382 WARM + 283 COLD) jamais pollés en mode workers v1
- Root cause: priority queue ne rescore que le top du heap ; HOT toujours
  au top → WARM/COLD enterrés jamais escaladés.

Design WRR (operator brief 2026-05-18):
- 3 queues séparées par lane (HOT, WARM, COLD)
- Budget alloué par worker via round-robin pondéré:
  HOT 47% (≈ 8.5 polls/s), WARM 47% (≈ 8.5), COLD 6% (≈ 1.0)
  Total ≤ 18 polls/s (rate cap respecté via shared TokenBucket)
- Workers pickent next lane via deficit-round-robin: la lane avec le
  plus grand déficit de polls (vs son quota) est servie d'abord.
- Anti-starvation IMPLICITE: aucune lane ne peut être bloquée car chaque
  lane a un quota positif minimum.
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

DEFAULT_WORKERS = 8
DEFAULT_HOT_INTERVAL_S = 10.0
DEFAULT_WARM_INTERVAL_S = 45.0
DEFAULT_COLD_INTERVAL_S = 120.0

# Default budget weights (sum can be anything, normalized internally).
# Tuned for 80/382/283 cohort, 18/s rate cap:
# HOT needs 8.0/s, WARM 8.5/s, COLD 2.4/s → total 18.9/s vs cap 18 → tight.
# WRR weights bring HOT down to share evenly with WARM, COLD gets minimum.
DEFAULT_WEIGHT_HOT = 47
DEFAULT_WEIGHT_WARM = 47
DEFAULT_WEIGHT_COLD = 6


def is_enabled() -> bool:
    try:
        return os.environ.get("POLLING_WORKERS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def workers_count() -> int:
    try:
        return max(1, min(32, int(os.environ.get("POLLING_WORKERS_COUNT", str(DEFAULT_WORKERS)))))
    except Exception:
        return DEFAULT_WORKERS


def _weights() -> tuple[int, int, int]:
    """Read budget weights from env, fallback to defaults."""
    try:
        h = int(os.environ.get("POLLING_WRR_WEIGHT_HOT", str(DEFAULT_WEIGHT_HOT)))
        w = int(os.environ.get("POLLING_WRR_WEIGHT_WARM", str(DEFAULT_WEIGHT_WARM)))
        c = int(os.environ.get("POLLING_WRR_WEIGHT_COLD", str(DEFAULT_WEIGHT_COLD)))
        return max(1, h), max(1, w), max(1, c)
    except Exception:
        return DEFAULT_WEIGHT_HOT, DEFAULT_WEIGHT_WARM, DEFAULT_WEIGHT_COLD


@dataclass(order=True)
class _LaneItem:
    """One heap entry: ordered by next_due (no priority — lane-local heap)."""
    next_due: float
    addr: str = field(compare=False)


class WrrScheduler:
    """Weighted Round Robin scheduler with 3 lane-local heaps.

    Each lane has its own heap of (next_due, addr). Workers call
    ``get_next_due()`` which picks a lane via deficit-round-robin (the lane
    most behind its quota), waits for the next due item in that lane, pops
    and returns. After processing, worker requeues via ``put``.

    Anti-starvation: a lane that's behind quota gets priority. HOT cannot
    drown WARM/COLD because each lane has minimum guaranteed throughput.
    """

    def __init__(self, weight_hot: int, weight_warm: int, weight_cold: int) -> None:
        self._heaps: dict[str, list[_LaneItem]] = {"HOT": [], "WARM": [], "COLD": []}
        self._weights: dict[str, int] = {"HOT": weight_hot, "WARM": weight_warm, "COLD": weight_cold}
        self._served: dict[str, int] = {"HOT": 0, "WARM": 0, "COLD": 0}
        self._cond = asyncio.Condition()
        self._stopped = False

    def _deficit(self, lane: str) -> float:
        """How much THIS lane is behind its quota (normalized).
        Higher deficit = lane more starved = pick it next."""
        total_served = sum(self._served.values()) or 1
        total_weight = sum(self._weights.values())
        expected_share = self._weights[lane] / total_weight
        actual_share = self._served[lane] / total_served
        return expected_share - actual_share

    def _pick_lane(self) -> str | None:
        """Pick lane with highest deficit AND non-empty heap with due item.
        Returns None if no lane has a due item right now."""
        now = asyncio.get_event_loop().time()
        candidates = []
        for lane, heap in self._heaps.items():
            if not heap:
                continue
            top = heap[0]
            if top.next_due <= now:
                candidates.append((self._deficit(lane), lane))
        if not candidates:
            return None
        # Max deficit wins
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    def _next_due_overall(self) -> float | None:
        """Earliest next_due across all lanes. None if all heaps empty."""
        nexts = [h[0].next_due for h in self._heaps.values() if h]
        return min(nexts) if nexts else None

    async def put(self, addr: str, lane: str, next_due: float) -> None:
        async with self._cond:
            if lane not in self._heaps:
                lane = "WARM"  # safety fallback
            heapq.heappush(self._heaps[lane], _LaneItem(next_due, addr))
            self._cond.notify()

    async def get_when_due(self) -> tuple[str, str]:
        """Block until next item is due via WRR. Returns (addr, lane)."""
        while True:
            async with self._cond:
                if self._stopped:
                    raise asyncio.CancelledError("scheduler stopped")
                # Try to pick lane with due item
                lane = self._pick_lane()
                if lane is not None:
                    item = heapq.heappop(self._heaps[lane])
                    self._served[lane] += 1
                    return item.addr, lane
                # No lane has a due item — wait for next due overall or notify
                nd = self._next_due_overall()
                if nd is None:
                    # All heaps empty — wait for put
                    await self._cond.wait()
                    continue
                wait = max(0.0, nd - asyncio.get_event_loop().time())
            try:
                await asyncio.wait_for(self._notify_event(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def _notify_event(self) -> None:
        async with self._cond:
            await self._cond.wait()

    async def stop(self) -> None:
        async with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def size(self) -> int:
        return sum(len(h) for h in self._heaps.values())

    def size_by_lane(self) -> dict[str, int]:
        return {lane: len(h) for lane, h in self._heaps.items()}

    def served_by_lane(self) -> dict[str, int]:
        return dict(self._served)


@dataclass
class WrrWorkerPool:
    """N workers consuming a WrrScheduler. Same API as v1 WorkerPool.

    P0-C 2026-05-19 — phase timing instrumentation. ``stats["phase_total_s"]``
    accumulates seconds across all polls per phase:
    - ``wait_token`` : time spent in ``rate_limiter.acquire()``
    - ``poll_func`` : time spent in poll_func (fetch + process trades + DB)
    - ``requeue`` : time spent in ``scheduler.put`` requeue
    Divide by ``polls_done`` to get avg phase ms per poll. Used to drill
    bottleneck (P0-C cadence chantier).
    """
    scheduler: WrrScheduler
    rate_limiter: Any
    poll_func: Callable[[str], Awaitable[Any]]
    interval_func: Callable[[str], float]
    n_workers: int = DEFAULT_WORKERS
    stats: dict[str, Any] = field(default_factory=lambda: {
        "polls_done": 0,
        "errors": 0,
        "by_lane": {"HOT": 0, "WARM": 0, "COLD": 0},
        "phase_total_s": {"wait_token": 0.0, "poll_func": 0.0, "requeue": 0.0},
        "polls_with_trade": 0,
        "polls_without_trade": 0,
        "poll_func_s_with_trade": 0.0,
        "poll_func_s_without_trade": 0.0,
    })
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def _worker(self, worker_id: int) -> None:
        import time as _time
        logger.info("wrr_worker %d started", worker_id)
        try:
            while not self._stop_event.is_set():
                try:
                    addr, lane = await self.scheduler.get_when_due()
                except asyncio.CancelledError:
                    break
                try:
                    # P0-D 2026-05-19 fix double-rate-limiter : the rate cap
                    # is enforced INSIDE fetch_recent_activity (which makes
                    # the actual Polymarket API call). Acquiring here too
                    # consumed 2 tokens per poll → effective cap 9/s instead
                    # of 18/s. Now wait_token measures only the dispatch
                    # latency (should be ~0) ; the real API rate cap lives
                    # at the call site. rate_limiter kept for legacy/test
                    # API surface but no acquire here.
                    t0 = _time.perf_counter()
                    t1 = t0  # no token acquire here
                    poll_result = await self.poll_func(addr)
                    t2 = _time.perf_counter()
                    # Accumulate phase times + counters
                    self.stats["phase_total_s"]["wait_token"] += t1 - t0
                    poll_duration = t2 - t1
                    self.stats["phase_total_s"]["poll_func"] += poll_duration
                    self.stats["polls_done"] += 1
                    self.stats["by_lane"].setdefault(lane, 0)
                    self.stats["by_lane"][lane] += 1
                    # Distinguish polls that returned new trades vs empty polls.
                    # poll_func currently returns None; we infer trade detection
                    # from runtime side-effects via duration heuristic if needed.
                    # For now classify by duration : >250ms likely has trade work.
                    if poll_duration > 0.25:
                        self.stats["polls_with_trade"] += 1
                        self.stats["poll_func_s_with_trade"] += poll_duration
                    else:
                        self.stats["polls_without_trade"] += 1
                        self.stats["poll_func_s_without_trade"] += poll_duration
                except Exception as exc:
                    self.stats["errors"] += 1
                    logger.warning(
                        "wrr_worker err worker=%d wallet=%s lane=%s err=%r",
                        worker_id, addr, lane, exc,
                    )
                finally:
                    try:
                        t3 = _time.perf_counter()
                        interval = self.interval_func(lane)
                        next_due = asyncio.get_event_loop().time() + interval
                        await self.scheduler.put(addr, lane, next_due)
                        t4 = _time.perf_counter()
                        self.stats["phase_total_s"]["requeue"] += t4 - t3
                    except Exception as exc:
                        logger.warning("wrr_worker requeue err worker=%d wallet=%s err=%r", worker_id, addr, exc)
        finally:
            logger.info("wrr_worker %d stopped", worker_id)

    async def start(self) -> None:
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._worker(i), name=f"wrr_worker_{i}")
            for i in range(self.n_workers)
        ]
        logger.info("WrrWorkerPool started with %d workers", self.n_workers)

    async def stop(self) -> None:
        self._stop_event.set()
        await self.scheduler.stop()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("WrrWorkerPool stopped. Final stats: %s", self.stats)
