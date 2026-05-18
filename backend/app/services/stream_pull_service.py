"""Stream-pull polling — 2026-05-16 architecture upgrade.

Replaces per-wallet polling latency bottleneck (491 wallets / 15 calls/s
= 33s cycle) with a single batched `/trades?limit=1000` call covering
~60s of GLOBAL Polymarket activity in 1 call.

Bench confirmed (2026-05-16) :
- /trades?limit=1000 returns up to 1000 most recent trades (all wallets,
  all markets), covering ~60s of activity.
- Single call latency p50 = 48ms.
- Polling 1 call / 10s = 0.1 req/s = quasi-zero rate-limit budget.
- Cycle effective per wallet = 10s vs 33s with per-wallet polling.

Architecture :
- Background asyncio task in same process as bot.
- Polls /trades?limit=stream_pull_limit every stream_pull_interval_s.
- Filters trades dont `proxyWallet` ∈ current polling cohort (live read).
- Dispatches each matched trade to existing `_process_trade_in_session()`
  via `asyncio.to_thread` (same pattern as per-wallet polling).
- Dedup via in-memory LRU of transactionHash to prevent double-processing
  (defensive — audit pipeline is already idempotent via merge upsert).
- Coexists with per-wallet polling (fallback / redundancy). Doctrine :
  this service ADDS coverage, never REMOVES safety.

Doctrine respected :
- Zero change to audit logic.
- Zero change to gates / cohort / tier rules / sizing.
- Zero change to risk_engine / paper_trading_engine.maybe_auto_trade.
- New service only adds a faster signal-detection lane.
- Feature flag `stream_pull_enabled` (default False) for clean rollback.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


STREAM_URL = "https://data-api.polymarket.com/trades"
STREAM_PULL_ENABLED = _env_bool("STREAM_PULL_ENABLED", False)
DEFAULT_LIMIT = _env_int("STREAM_PULL_LIMIT", 1000)
DEFAULT_INTERVAL_S = _env_int("STREAM_PULL_INTERVAL_S", 10)
# Operator rule 2026-05-16 : skip trades older than this before dispatch.
# Avoids polluting audit with STALE_SIGNAL_BACKFILL rejects. 90s aligns
# with crypto-5min freshness threshold per spec.md.
DEFAULT_MAX_TRADE_AGE_S = _env_int("STREAM_PULL_MAX_TRADE_AGE_S", 90)
HTTP_TIMEOUT_S = 8.0
DEDUP_LRU_SIZE = 10_000


class StreamPullService:
    """Pulls global recent trades, filters cohort, dispatches existing pipeline."""

    def __init__(
        self,
        polling_engine: Any,
        *,
        interval_s: int = DEFAULT_INTERVAL_S,
        limit: int = DEFAULT_LIMIT,
        max_trade_age_s: int = DEFAULT_MAX_TRADE_AGE_S,
    ) -> None:
        self.polling_engine = polling_engine
        self.interval_s = interval_s
        self.limit = limit
        self.max_trade_age_s = max_trade_age_s
        self._processed_ids: deque[str] = deque(maxlen=DEDUP_LRU_SIZE)
        self._processed_id_set: set[str] = set()
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        # Counters (read by /observability or status endpoint).
        self.cycles_completed = 0
        self.cycles_failed = 0
        self.trades_seen_total = 0
        self.trades_matched_cohort = 0
        self.trades_dedup_skipped = 0
        self.trades_skipped_too_old = 0
        self.last_skipped_too_old_age_s: Optional[int] = None
        self.trades_dispatched = 0
        self.paper_executed = 0
        self.last_cycle_at: Optional[float] = None
        self.last_error: Optional[str] = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running():
            logger.warning("stream_pull: already running, skip start")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="stream_pull_loop")
        logger.info(
            "stream_pull: started interval=%ds limit=%d url=%s",
            self.interval_s, self.limit, STREAM_URL,
        )

    async def stop(self) -> None:
        if not self.is_running():
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=self.interval_s + 2.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
        logger.info("stream_pull: stopped")

    async def _run_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while not self._stop_event.is_set():
                try:
                    await self._fetch_and_process(client)
                    self.cycles_completed += 1
                except Exception as exc:
                    self.cycles_failed += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("stream_pull cycle err: %s", self.last_error)
                # Sleep with cancellation responsiveness
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.interval_s
                    )
                except asyncio.TimeoutError:
                    pass

    async def _fetch_and_process(self, client: httpx.AsyncClient) -> None:
        import time
        t0 = time.monotonic()
        r = await client.get(
            STREAM_URL, params={"limit": self.limit}, timeout=HTTP_TIMEOUT_S
        )
        r.raise_for_status()
        trades = r.json()
        if not isinstance(trades, list):
            return
        self.trades_seen_total += len(trades)
        # Per-cycle counters for visibility into where dispatches land
        cycle_matched = 0
        cycle_dispatched = 0
        cycle_executed = 0
        cycle_rejected = 0
        cycle_reasons: dict[str, int] = {}

        # Live-read cohort. Polling engine maintains it (refresh on weekly
        # reclass or post-apply). Take a snapshot to a set for O(1) lookup.
        cohort_list = self.polling_engine._cohort or []
        cohort_set = {a.lower() for a in cohort_list}
        if not cohort_set:
            return

        # Process trades oldest-first to maintain temporal ordering.
        trades.sort(key=lambda t: int(t.get("timestamp") or 0))

        for trade in trades:
            tid = trade.get("transactionHash") or trade.get("id")
            if not tid:
                continue
            if tid in self._processed_id_set:
                self.trades_dedup_skipped += 1
                continue
            wallet = (trade.get("proxyWallet") or "").lower()
            if not wallet or wallet not in cohort_set:
                continue
            self.trades_matched_cohort += 1
            cycle_matched += 1
            # 2026-05-16 patch : age filter avant dispatch.
            # Au-delà de max_trade_age_s, le pipeline rejette via
            # STALE_SIGNAL_BACKFILL → waste CPU + DB. Skip propre ici.
            trade_ts = int(trade.get("timestamp") or 0)
            now_ts = int(time.time())
            if trade_ts and (now_ts - trade_ts) > self.max_trade_age_s:
                self.trades_skipped_too_old += 1
                self.last_skipped_too_old_age_s = now_ts - trade_ts
                self._processed_ids.append(tid)
                self._processed_id_set.add(tid)
                continue
            # Use the EXACT same normalizer + processor as per-wallet polling.
            normalized = self.polling_engine._raw_to_audit_input(trade, wallet)
            # P0-B 2026-05-18 — log-only delay observer (no-op if flag off)
            from app.services import polling_delay_observer as _pdo
            _obs = _pdo.start_observation(normalized)
            try:
                result = None
                try:
                    result = await asyncio.to_thread(
                        self.polling_engine._process_trade_in_session, normalized
                    )
                finally:
                    _pdo.finalize_from_result(_obs, result)
                self.trades_dispatched += 1
                cycle_dispatched += 1
                # Mirror polling counters for observability parity.
                try:
                    self.polling_engine._trades_detected_session += 1
                except Exception:
                    pass
                if result.get("executed"):
                    self.paper_executed += 1
                    cycle_executed += 1
                    try:
                        self.polling_engine._paper_trades_session += 1
                    except Exception:
                        pass
                else:
                    cycle_rejected += 1
                    reason = str(result.get("reason") or "UNKNOWN")[:64]
                    cycle_reasons[reason] = cycle_reasons.get(reason, 0) + 1
            except Exception as exc:
                logger.warning(
                    "stream_pull: process failed tid=%s wallet=%s err=%s",
                    tid[:16], wallet[:10], exc,
                )
            finally:
                # Mark as processed regardless of success — avoid retry storm.
                self._processed_ids.append(tid)
                self._processed_id_set.add(tid)
                # Trim set if LRU deque rotated out
                if len(self._processed_id_set) > DEDUP_LRU_SIZE + 100:
                    # Rebuild from deque (cheap, runs rarely)
                    self._processed_id_set = set(self._processed_ids)

        self.last_cycle_at = t0
        # Per-cycle log (WARN level so it's visible without INFO filter)
        if cycle_matched > 0 or cycle_dispatched > 0:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "stream_pull cycle: trades_seen=%d cohort_size=%d matched=%d "
                "dispatched=%d executed=%d rejected=%d reasons=%s elapsed=%dms",
                len(trades), len(cohort_set), cycle_matched, cycle_dispatched,
                cycle_executed, cycle_rejected, cycle_reasons, elapsed_ms,
            )


_INSTANCE: Optional[StreamPullService] = None


def get_stream_pull_service() -> Optional[StreamPullService]:
    return _INSTANCE


def init_stream_pull_service(polling_engine: Any, **kwargs) -> StreamPullService:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = StreamPullService(polling_engine, **kwargs)
    return _INSTANCE
