"""P0-D (2026-05-19) — Two-stage polling/audit separation.

Diagnostic P0-C 15min (2026-05-19 00:07-00:22 UTC) :
- polls/s = 10.34 (57.5% of cap 18/s)
- wait_token = 488ms (55% du cycle)
- poll_func = 399ms (45%) — with_trade variant = 510ms, audit_trade p50 = 380ms
- poll_func_without_trade = 192ms

Conclusion : audit_trade DB writes + signal/paper engine sont la moitié du
worker cycle. En extrayant ce travail dans des audit workers asynchrones,
les pollers peuvent rester proches du cap 18/s.

Design :
- Stage 1 pollers : fetch /activity → dedupe → push event → return fast.
  N=8 workers, shared TokenBucket (rate cap 18/s).
- Stage 2 audit workers : pop event → process_trade_in_session (unchanged).
  N=4 workers, no rate limit, pure CPU/DB bound.
- Bounded asyncio.Queue (maxsize=200). Backpressure : if full, log dead-letter
  to JSONL + skip (never block poller).
- Kill switch : if queue.qsize() > kill_threshold, log + alert (no auto-kill
  to preserve operator control).

Doctrine preserved :
- _process_trade_in_session body UNCHANGED. Just relocated to audit worker.
- copyable_edge / gates / R / reclass / duration / live : unchanged.
- Idempotency (audit_id = audit-{trade_id}) : already in pipeline.
- No-duplicate via has_open_position_for_market_outcome : already in paper engine.
- Paper/live invariant : same _process_trade_in_session for both modes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


DEFAULT_AUDIT_WORKERS = 4
DEFAULT_QUEUE_MAXSIZE = 200
DEFAULT_KILL_THRESHOLD = 150  # alert + dead-letter if queue >= this
DEAD_LETTER_DIR = Path(__file__).resolve().parents[3] / "data" / "_two_stage_deadletter"


def is_enabled() -> bool:
    try:
        return os.environ.get("POLLING_TWO_STAGE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def audit_workers_count() -> int:
    try:
        return max(1, min(16, int(os.environ.get("POLLING_AUDIT_WORKERS", str(DEFAULT_AUDIT_WORKERS)))))
    except Exception:
        return DEFAULT_AUDIT_WORKERS


def queue_maxsize() -> int:
    try:
        return max(50, int(os.environ.get("POLLING_AUDIT_QUEUE_MAX", str(DEFAULT_QUEUE_MAXSIZE))))
    except Exception:
        return DEFAULT_QUEUE_MAXSIZE


def _dead_letter(event: dict[str, Any], reason: str) -> None:
    """Append dropped event to JSONL for forensic recovery."""
    try:
        DEAD_LETTER_DIR.mkdir(parents=True, exist_ok=True)
        hour = datetime.now(timezone.utc).strftime("%Y%m%dT%HZ")
        path = DEAD_LETTER_DIR / f"deadletter_{hour}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "dropped_at_ms": int(time.time() * 1000),
                "reason": reason,
                "event": event,
            }, default=str) + "\n")
    except Exception as exc:
        logger.debug("dead_letter write failed: %r", exc)


@dataclass
class AuditQueue:
    """Wrapper around asyncio.Queue with backpressure + dead-letter."""
    maxsize: int = DEFAULT_QUEUE_MAXSIZE
    kill_threshold: int = DEFAULT_KILL_THRESHOLD
    _queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=DEFAULT_QUEUE_MAXSIZE))
    stats: dict[str, int] = field(default_factory=lambda: {
        "enqueued": 0,
        "dropped_full": 0,
        "dequeued": 0,
        "kill_threshold_hits": 0,
    })

    def __post_init__(self):
        self._queue = asyncio.Queue(maxsize=self.maxsize)

    async def put_nowait_drop(self, event: dict[str, Any]) -> bool:
        """Try to enqueue without blocking. If full, dead-letter the event.
        Returns True if enqueued, False if dropped."""
        try:
            self._queue.put_nowait(event)
            self.stats["enqueued"] += 1
            if self._queue.qsize() >= self.kill_threshold:
                self.stats["kill_threshold_hits"] += 1
                logger.warning(
                    "audit_queue qsize=%d >= kill_threshold=%d — operator alert",
                    self._queue.qsize(), self.kill_threshold,
                )
            return True
        except asyncio.QueueFull:
            self.stats["dropped_full"] += 1
            _dead_letter(event, "queue_full")
            return False

    async def get(self) -> dict[str, Any]:
        event = await self._queue.get()
        self.stats["dequeued"] += 1
        return event

    def qsize(self) -> int:
        return self._queue.qsize()


@dataclass
class AuditWorkerPool:
    """N audit workers consuming AuditQueue. Each calls process_func(event).
    No rate limit (audit is CPU/DB bound, not API bound).
    """
    queue: AuditQueue
    process_func: Callable[[dict[str, Any]], Awaitable[Any]]
    n_workers: int = DEFAULT_AUDIT_WORKERS
    stats: dict[str, Any] = field(default_factory=lambda: {
        "processed": 0,
        "errors": 0,
        "phase_total_s": {"audit_work": 0.0},
    })
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def _worker(self, worker_id: int) -> None:
        logger.info("audit_worker %d started", worker_id)
        try:
            while not self._stop_event.is_set():
                try:
                    event = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                t0 = time.perf_counter()
                try:
                    await self.process_func(event)
                    self.stats["processed"] += 1
                except Exception as exc:
                    self.stats["errors"] += 1
                    logger.warning("audit_worker err worker=%d err=%r", worker_id, exc)
                finally:
                    self.stats["phase_total_s"]["audit_work"] += time.perf_counter() - t0
        finally:
            logger.info("audit_worker %d stopped (processed=%d errors=%d)",
                        worker_id, self.stats["processed"], self.stats["errors"])

    async def start(self) -> None:
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._worker(i), name=f"audit_worker_{i}")
            for i in range(self.n_workers)
        ]
        logger.info("AuditWorkerPool started with %d workers", self.n_workers)

    async def stop(self) -> None:
        self._stop_event.set()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("AuditWorkerPool stopped. Final stats: %s", self.stats)
