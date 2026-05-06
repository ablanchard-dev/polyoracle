"""v0.7.8 Phase 6 — Latency instrumentation.

Tracks p50/p95/max per pipeline path so we can:
  1. Verify the latency budget (Vision Lock §4) is met
  2. Detect regressions (e.g. Gamma fetch suddenly 3× slower)
  3. Trigger alerts when a path exceeds 2× budget for >5min
  4. Report daily latency dashboard

Design:
  - Singleton per process — `get_tracker()`
  - Each measurement: `(path_name, elapsed_ms, timestamp)` appended to a
    bounded ring buffer (default 10k samples per path)
  - Quantiles computed on-demand (cheap for ≤10k samples)
  - Per-path budget enforcement: if p95 > 2× budget, set `breach=True`
    flag visible via `/latency/status`

Paths instrumented (Vision Lock §4):
  - polling_fetch_per_wallet
  - audit_trade
  - cluster_check
  - allocator_evaluate
  - paper_open_total (incl. live price re-fetch)
  - resolver_resolve
  - close_loop_iteration

Usage:
  from app.services.latency_tracker import track_latency
  with track_latency("audit_trade"):
      ...

Or:
  start = time.monotonic()
  ...
  get_tracker().record("audit_trade", (time.monotonic() - start) * 1000)
"""

from __future__ import annotations

import bisect
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# Latency budgets per Vision Lock §4 (ms)
LATENCY_BUDGET_MS: dict[str, float] = {
    "polling_fetch_per_wallet": 30_000.0,   # 30s — = polling interval ELITE
    "audit_trade": 500.0,                    # < 500ms incl. resolver
    "cluster_check": 100.0,
    "allocator_evaluate": 50.0,
    "paper_open_total": 800.0,               # incl. live Gamma price re-fetch
    "resolver_resolve": 1500.0,              # hard timeout
    "close_loop_iteration": 200.0,           # cheap if no positions due
    "live_order_submit": 500.0,              # CLOB ack budget (Phase 7)
}

# Ring buffer size per path
RING_BUFFER_SIZE: int = 10_000

# Breach threshold: if p95 > BREACH_MULTIPLIER × budget, flag
BREACH_MULTIPLIER: float = 2.0


@dataclass
class _PathStats:
    samples: deque[float]  # elapsed_ms ring buffer
    last_sample_at: float = 0.0
    breach: bool = False


class LatencyTracker:
    """Per-process singleton tracking latency across pipeline paths.

    Thread-safe. Quantile computation is O(N log N) on the ring buffer
    — cheap for N ≤ 10k.
    """

    def __init__(self) -> None:
        self._paths: dict[str, _PathStats] = {}
        self._lock = threading.Lock()

    def record(self, path: str, elapsed_ms: float) -> None:
        """Record a single sample. Called by the `track_latency` context
        manager or directly by code that already times itself."""
        if elapsed_ms < 0:
            return  # invalid sample (clock skew?)
        with self._lock:
            stats = self._paths.get(path)
            if stats is None:
                stats = _PathStats(samples=deque(maxlen=RING_BUFFER_SIZE))
                self._paths[path] = stats
            stats.samples.append(float(elapsed_ms))
            stats.last_sample_at = time.monotonic()

            # Recompute breach flag on every Nth sample
            if len(stats.samples) >= 50 and len(stats.samples) % 50 == 0:
                stats.breach = self._compute_breach(path, stats)

    def quantiles(self, path: str) -> dict[str, float]:
        """Return p50/p95/max/n for the given path."""
        with self._lock:
            stats = self._paths.get(path)
            if stats is None or not stats.samples:
                return {"p50": 0.0, "p95": 0.0, "max": 0.0, "n": 0, "breach": False}
            samples = sorted(stats.samples)
            n = len(samples)
            return {
                "p50": _percentile(samples, 0.50),
                "p95": _percentile(samples, 0.95),
                "max": samples[-1],
                "n": n,
                "breach": stats.breach,
            }

    def all_paths_status(self) -> dict[str, dict[str, float]]:
        """Return quantiles for every recorded path. For /latency endpoint."""
        with self._lock:
            paths = list(self._paths.keys())
        return {p: self.quantiles(p) for p in paths}

    def reset(self, path: Optional[str] = None) -> None:
        """Clear samples — for tests."""
        with self._lock:
            if path is None:
                self._paths.clear()
            else:
                self._paths.pop(path, None)

    def _compute_breach(self, path: str, stats: _PathStats) -> bool:
        budget = LATENCY_BUDGET_MS.get(path)
        if budget is None:
            return False
        samples = sorted(stats.samples)
        p95 = _percentile(samples, 0.95)
        breach = p95 > BREACH_MULTIPLIER * budget
        if breach and not stats.breach:
            logger.warning(
                "latency breach: path=%s p95=%.0fms budget=%.0fms (%.1fx)",
                path, p95, budget, p95 / budget,
            )
        return breach


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Standard linear-interpolation percentile."""
    if not sorted_samples:
        return 0.0
    n = len(sorted_samples)
    if n == 1:
        return sorted_samples[0]
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac


# ---------------- module-level singleton ----------------

_DEFAULT_TRACKER: LatencyTracker | None = None
_DEFAULT_LOCK = threading.Lock()


def get_tracker() -> LatencyTracker:
    global _DEFAULT_TRACKER
    with _DEFAULT_LOCK:
        if _DEFAULT_TRACKER is None:
            _DEFAULT_TRACKER = LatencyTracker()
    return _DEFAULT_TRACKER


def reset_tracker_for_tests() -> None:
    global _DEFAULT_TRACKER
    with _DEFAULT_LOCK:
        _DEFAULT_TRACKER = None


# ---------------- context manager API ----------------


@contextmanager
def track_latency(path: str):
    """Context manager that records the elapsed time of the wrapped block.

    Usage:
        with track_latency("audit_trade"):
            audit_engine.audit_trade(raw)
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        get_tracker().record(path, elapsed_ms)


# ---------------- daily report helpers ----------------


def daily_report() -> str:
    """Generate a markdown latency report for the current state. Used by
    the /reports/daily endpoint and the daily cron summary."""
    tracker = get_tracker()
    status = tracker.all_paths_status()
    if not status:
        return "# Latency Report — no samples recorded yet\n"

    lines = ["# Latency Report — v0.7.8\n"]
    lines.append("| path | n | p50 (ms) | p95 (ms) | max (ms) | budget (ms) | ratio | breach |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|:---:|")
    for path in sorted(status.keys()):
        stats = status[path]
        budget = LATENCY_BUDGET_MS.get(path, 0)
        ratio = stats["p95"] / budget if budget > 0 else 0
        breach_str = "❌" if stats["breach"] else "✅"
        lines.append(
            f"| {path} | {stats['n']} | {stats['p50']:.1f} | "
            f"{stats['p95']:.1f} | {stats['max']:.1f} | "
            f"{budget:.0f} | {ratio:.2f}× | {breach_str} |"
        )
    return "\n".join(lines)
