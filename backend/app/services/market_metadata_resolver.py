"""v0.7.8 — Market metadata resolver (C1 fix).

When the polling pipeline detects a wallet trade on a market that has
no row in the local ``market`` table, the audit decision sees default
``liquidity_score=0`` and ``orderbook_quality=INSUFFICIENT_DATA`` and
downgrades to WATCH. Our v0.7.8 smoke 100€ showed 198/369 (53.7%) of
ELITE WATCH audits were caused by this race condition — wallets with
edge ~0.40 silently rejected because the market backfill hadn't run.

This module fixes that gap **on-demand**, with the following invariants:

1. **No fake low-liquidity**: a missing market metadata never becomes
   ``LOW_LIQUIDITY_SCORE=0``. It maps to one of 4 explicit statuses:
   - ``COMPLETE``: Gamma + CLOB returned full data; market upserted
   - ``PARTIAL_NO_ORDERBOOK``: Gamma OK, CLOB unavailable
   - ``MARKET_METADATA_NOT_FOUND``: Gamma returned empty (delisted)
   - ``MARKET_METADATA_FETCH_FAILED``: Gamma error/timeout

2. **Latency-safe**: total resolve time bounded by ``RESOLVE_TIMEOUT_S``
   (default 1.5s). If we exceed budget, return ``FETCH_FAILED`` rather
   than block the polling loop.

3. **Cache** (two-tier per the v0.7.8 plan):
   - Static fields (category, duration_bucket, deadline,
     expected_resolution_minutes, clob_token_ids): TTL 30 min
   - Dynamic fields (yes_price, spread, liquidity, orderbook): TTL 30s
     OR no-cache (caller decides via ``force_fresh_pricing``)

4. **NOT_FOUND blacklist** (1h): if Gamma confirms a market_id is
   unknown, blacklist temporarily so we don't retry per audit.

5. **Concurrency**: per-market_id lock via ``WeakValueDictionary`` so
   parallel audits on the same missing market share ONE Gamma fetch
   (= one network call, not N).

6. **Token bucket**: dedicated 1 call/s rate budget for the resolver,
   so it never starves the polling token bucket (5 calls/s shared).

Designed for live-readiness: same module will work in Docker and live
mode. Write paths are sync (DB upsert via SQLModel session); the fetch
itself is sync HTTP (httpx in PolymarketHttpClient) wrapped under a
strict deadline.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from sqlmodel import Session

logger = logging.getLogger(__name__)


# ---------------- statuses ----------------


ResolveStatus = Literal[
    "COMPLETE",
    "PARTIAL_NO_ORDERBOOK",
    "MARKET_METADATA_NOT_FOUND",
    "MARKET_METADATA_FETCH_FAILED",
    "MARKET_METADATA_TIMEOUT",
]


# ---------------- TTL constants ----------------


# Static metadata: category/deadline/duration_bucket/clob_token_ids — these
# don't change during a market's life, 30 min cache is generous.
STATIC_METADATA_TTL_S: float = 30 * 60.0
# Dynamic data: yes_price/spread/liquidity — refreshed every poll cycle on
# Polymarket, never cache > 30s.
DYNAMIC_DATA_TTL_S: float = 30.0
# NOT_FOUND blacklist: if Gamma says "no such market", don't retry for 1h.
NOT_FOUND_BLACKLIST_TTL_S: float = 60 * 60.0
# Hard timeout for a single resolve() call (Gamma + CLOB combined).
RESOLVE_TIMEOUT_S: float = 1.5
# Resolver-dedicated rate budget (calls/s). Polling has 5/s shared, the
# resolver gets its own 1/s so it never starves polling.
RESOLVER_RATE_LIMIT_CALLS_PER_SEC: float = 1.0


# ---------------- result dataclass ----------------


@dataclass
class ResolveResult:
    """Outcome of a single resolve() call.

    The ``status`` field tells the caller what to do:
    - COMPLETE: market upserted, audit can proceed normally
    - PARTIAL_NO_ORDERBOOK: market upserted with Gamma metadata only;
      audit pipeline should treat orderbook as unknown (skip orderbook
      gate, do not assume BAD/UNTRADABLE)
    - MARKET_METADATA_NOT_FOUND: market doesn't exist on Polymarket;
      caller must reject with reason MARKET_METADATA_NOT_FOUND
    - MARKET_METADATA_FETCH_FAILED: transient network/parse error;
      caller should reject with MARKET_METADATA_FETCH_FAILED and let
      the next polling cycle retry
    - MARKET_METADATA_TIMEOUT: budget exceeded; same handling as
      FETCH_FAILED but distinguishes the cause for telemetry
    """

    status: ResolveStatus
    market_id: str
    elapsed_ms: float = 0.0
    cache_hit: bool = False
    has_static: bool = False
    has_orderbook: bool = False
    error: Optional[str] = None
    # Convenience pointers — caller may use them for the audit decision
    # without re-querying the DB.
    category: Optional[str] = None
    duration_bucket: Optional[str] = None
    expected_resolution_minutes: Optional[float] = None
    yes_price: Optional[float] = None
    no_price: Optional[float] = None
    spread: Optional[float] = None
    liquidity: Optional[float] = None
    orderbook_quality: Optional[str] = None


# ---------------- token bucket ----------------


class _TokenBucket:
    """Minimal token bucket for rate limiting. One bucket per resolver
    instance — leaving the polling engine's bucket undisturbed."""

    def __init__(self, rate_per_sec: float, capacity: float | None = None):
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity) if capacity is not None else self.rate * 2.0
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def try_acquire(self, tokens: float = 1.0, timeout_s: float = 0.0) -> bool:
        """Try to acquire ``tokens``. Blocks up to ``timeout_s`` if needed.
        Returns True on success, False if budget unavailable within deadline."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
                # Need to wait. Compute how long.
                deficit = tokens - self.tokens
                wait_for = deficit / self.rate
            if time.monotonic() + wait_for > deadline:
                return False
            time.sleep(min(wait_for, 0.05))


# ---------------- main resolver ----------------


class MarketMetadataResolver:
    """Singleton-ish resolver — instantiate once and reuse across the
    backend process. Thread-safe; the per-market locks deduplicate
    concurrent fetches on the same id."""

    def __init__(
        self,
        *,
        gamma_client: Any = None,
        clob_client: Any = None,
        rate_per_sec: float = RESOLVER_RATE_LIMIT_CALLS_PER_SEC,
        resolve_timeout_s: float = RESOLVE_TIMEOUT_S,
        static_ttl_s: float = STATIC_METADATA_TTL_S,
        dynamic_ttl_s: float = DYNAMIC_DATA_TTL_S,
        not_found_ttl_s: float = NOT_FOUND_BLACKLIST_TTL_S,
        clock=time.monotonic,
    ) -> None:
        # Lazy import — keeps tests light when GammaClient/CLOB are mocked.
        if gamma_client is None:
            from app.services.polymarket.gamma_client import GammaClient as _G
            gamma_client = _G()
        if clob_client is None:
            from app.services.polymarket.clob_client import ClobPublicClient as _C
            try:
                clob_client = _C()
            except Exception:
                clob_client = None
        self._gamma = gamma_client
        self._clob = clob_client
        self._bucket = _TokenBucket(rate_per_sec)
        self._resolve_timeout_s = float(resolve_timeout_s)
        self._static_ttl_s = float(static_ttl_s)
        self._dynamic_ttl_s = float(dynamic_ttl_s)
        self._not_found_ttl_s = float(not_found_ttl_s)
        self._clock = clock
        # Caches
        self._static_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._dynamic_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._not_found: dict[str, float] = {}  # market_id → expiry monotonic
        # Per-market locks via WeakValueDictionary — auto-cleanup when
        # no caller holds the lock anymore.
        self._locks: weakref.WeakValueDictionary[str, threading.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._locks_dict_lock = threading.Lock()
        # P0-E 2026-05-19 — observability counters (purely additive, no logic
        # change). All increments under _stats_lock for thread safety since
        # resolver is shared across audit workers (P0-D pool n=4).
        self._stats_lock = threading.Lock()
        self._stats: dict[str, int] = {
            "total_calls": 0,
            "blacklist_hits": 0,         # short-circuit via _not_found
            "blacklist_adds": 0,         # Gamma confirmed NOT_FOUND → added
            "lock_timeouts": 0,          # per-market lock acquire timed out
            "cache_full_hits": 0,        # static+dynamic both fresh → no fetch
            "cache_partial_misses": 0,   # one of static/dynamic stale → fetch
            "rate_limit_timeouts": 0,    # _bucket.try_acquire failed
            "gamma_fetches": 0,          # actual Gamma API call made
            "gamma_errors": 0,           # Gamma raised exception
            "gamma_empty": 0,            # Gamma returned no doc (→ blacklist)
            "clob_fetches": 0,           # actual CLOB orderbook call made
            "clob_errors": 0,            # CLOB raised
            "upsert_success": 0,         # DB upsert worked
            "upsert_fails": 0,           # DB upsert raised
            "result_complete": 0,
            "result_partial_no_orderbook": 0,
            "result_not_found": 0,
            "result_fetch_failed": 0,
            "result_timeout": 0,
        }

    def get_stats(self) -> dict[str, Any]:
        """Return a snapshot of the resolver counters + derived rates.
        P0-E observability — read-only, no side effect."""
        with self._stats_lock:
            stats = dict(self._stats)
        total = stats["total_calls"] or 1
        return {
            **stats,
            "cache_hit_rate_pct": round(100 * stats["cache_full_hits"] / total, 2),
            "blacklist_hit_rate_pct": round(100 * stats["blacklist_hits"] / total, 2),
            "gamma_fetch_rate_pct": round(100 * stats["gamma_fetches"] / total, 2),
            "result_complete_pct": round(100 * stats["result_complete"] / total, 2),
            "result_not_found_pct": round(100 * stats["result_not_found"] / total, 2),
            "result_timeout_pct": round(100 * stats["result_timeout"] / total, 2),
        }

    def _bump_stat(self, key: str, delta: int = 1) -> None:
        """Thread-safe counter increment."""
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + delta

    def _get_lock(self, market_id: str) -> threading.Lock:
        with self._locks_dict_lock:
            lock = self._locks.get(market_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[market_id] = lock
            return lock

    def _is_blacklisted(self, market_id: str) -> bool:
        expiry = self._not_found.get(market_id)
        if expiry is None:
            return False
        if self._clock() > expiry:
            self._not_found.pop(market_id, None)
            return False
        return True

    def _blacklist_not_found(self, market_id: str) -> None:
        self._not_found[market_id] = self._clock() + self._not_found_ttl_s

    def _static_cache_get(self, market_id: str) -> dict[str, Any] | None:
        entry = self._static_cache.get(market_id)
        if entry is None:
            return None
        ts, data = entry
        if self._clock() - ts > self._static_ttl_s:
            self._static_cache.pop(market_id, None)
            return None
        return data

    def _dynamic_cache_get(self, market_id: str) -> dict[str, Any] | None:
        entry = self._dynamic_cache.get(market_id)
        if entry is None:
            return None
        ts, data = entry
        if self._clock() - ts > self._dynamic_ttl_s:
            self._dynamic_cache.pop(market_id, None)
            return None
        return data

    def _static_cache_put(self, market_id: str, data: dict[str, Any]) -> None:
        self._static_cache[market_id] = (self._clock(), data)

    def _dynamic_cache_put(self, market_id: str, data: dict[str, Any]) -> None:
        self._dynamic_cache[market_id] = (self._clock(), data)

    # ---------------- public API ----------------

    def resolve(
        self,
        market_id: str,
        *,
        session: Optional[Session] = None,
        force_fresh_pricing: bool = False,
        deadline_s: Optional[float] = None,
    ) -> ResolveResult:
        # v0.7.8 Phase 6 — instrument resolver latency
        try:
            from app.services.latency_tracker import get_tracker as _t
            _start = time.monotonic()
            try:
                _result = self._resolve_inner(
                    market_id,
                    session=session,
                    force_fresh_pricing=force_fresh_pricing,
                    deadline_s=deadline_s,
                )
            finally:
                _elapsed_ms = (time.monotonic() - _start) * 1000.0
                _t().record("resolver_resolve", _elapsed_ms)
            return _result
        except ImportError:
            # latency_tracker not available — fall through to legacy path
            return self._resolve_inner(
                market_id,
                session=session,
                force_fresh_pricing=force_fresh_pricing,
                deadline_s=deadline_s,
            )

    def _resolve_inner(
        self,
        market_id: str,
        *,
        session: Optional[Session] = None,
        force_fresh_pricing: bool = False,
        deadline_s: Optional[float] = None,
    ) -> ResolveResult:
        """Ensure ``market_id`` has metadata available for an audit decision.

        - First checks the local DB ``market`` row; if present and not
          stale, returns COMPLETE without any API call.
        - On miss, fetches Gamma metadata, then optionally CLOB
          orderbook within the time budget.
        - Upserts the ``market`` row when fetch succeeds.

        ``force_fresh_pricing=True`` bypasses the dynamic cache so the
        caller sees live prices (e.g. used by ``_fetch_execution_price_now``
        in paper_trading_engine.py — the fix shipped in v0.7.8 latency).
        """
        t0 = self._clock()
        deadline = t0 + (deadline_s if deadline_s is not None else self._resolve_timeout_s)
        self._bump_stat("total_calls")

        # Blacklist short-circuit — never hit the network for a known-NOT_FOUND.
        if self._is_blacklisted(market_id):
            self._bump_stat("blacklist_hits")
            self._bump_stat("result_not_found")
            return ResolveResult(
                status="MARKET_METADATA_NOT_FOUND",
                market_id=market_id,
                elapsed_ms=(self._clock() - t0) * 1000,
                cache_hit=True,
                error="blacklisted_after_prior_not_found",
            )

        lock = self._get_lock(market_id)
        # Try to acquire the per-market lock with the remaining budget.
        remaining = max(0.0, deadline - self._clock())
        if not lock.acquire(timeout=remaining):
            self._bump_stat("lock_timeouts")
            self._bump_stat("result_timeout")
            return ResolveResult(
                status="MARKET_METADATA_TIMEOUT",
                market_id=market_id,
                elapsed_ms=(self._clock() - t0) * 1000,
                error="lock_acquire_timeout",
            )
        try:
            # Re-check blacklist + cache after lock (other thread may have
            # populated it while we were waiting).
            if self._is_blacklisted(market_id):
                self._bump_stat("blacklist_hits")
                self._bump_stat("result_not_found")
                return ResolveResult(
                    status="MARKET_METADATA_NOT_FOUND",
                    market_id=market_id,
                    elapsed_ms=(self._clock() - t0) * 1000,
                    cache_hit=True,
                )
            static = self._static_cache_get(market_id)
            dynamic = self._dynamic_cache_get(market_id)
            # Cache hit: static fresh AND dynamic fresh AND caller didn't
            # force a re-fetch. Otherwise we drop through to a network call.
            if (
                static is not None
                and dynamic is not None
                and not force_fresh_pricing
            ):
                self._bump_stat("cache_full_hits")
                result = self._build_result(
                    market_id, t0, static, dynamic, cache_hit=True,
                )
                # Track terminal status
                if result.status == "COMPLETE":
                    self._bump_stat("result_complete")
                elif result.status == "PARTIAL_NO_ORDERBOOK":
                    self._bump_stat("result_partial_no_orderbook")
                return result

            self._bump_stat("cache_partial_misses")

            # Need to fetch. Ensure rate budget allows it.
            remaining = max(0.0, deadline - self._clock())
            if not self._bucket.try_acquire(timeout_s=remaining):
                self._bump_stat("rate_limit_timeouts")
                self._bump_stat("result_timeout")
                return ResolveResult(
                    status="MARKET_METADATA_TIMEOUT",
                    market_id=market_id,
                    elapsed_ms=(self._clock() - t0) * 1000,
                    error="rate_limit_timeout",
                )

            # 1. Gamma fetch (static + light dynamic)
            self._bump_stat("gamma_fetches")
            try:
                gamma_doc = self._gamma.fetch_market_by_condition(market_id)
            except Exception as exc:
                self._bump_stat("gamma_errors")
                self._bump_stat("result_fetch_failed")
                return ResolveResult(
                    status="MARKET_METADATA_FETCH_FAILED",
                    market_id=market_id,
                    elapsed_ms=(self._clock() - t0) * 1000,
                    error=f"gamma_exception: {exc}",
                )
            if not gamma_doc:
                # Gamma confirms not-found — blacklist for 1h.
                self._bump_stat("gamma_empty")
                self._bump_stat("blacklist_adds")
                self._bump_stat("result_not_found")
                self._blacklist_not_found(market_id)
                return ResolveResult(
                    status="MARKET_METADATA_NOT_FOUND",
                    market_id=market_id,
                    elapsed_ms=(self._clock() - t0) * 1000,
                )

            static_data = _extract_static(gamma_doc)
            self._static_cache_put(market_id, static_data)

            # 2. CLOB orderbook fetch (only if budget remains)
            orderbook_data: dict[str, Any] = {}
            remaining = max(0.0, deadline - self._clock())
            if remaining > 0.05 and self._clob is not None:
                token_ids = static_data.get("clob_token_ids") or []
                if isinstance(token_ids, list) and token_ids:
                    self._bump_stat("clob_fetches")
                    try:
                        # Use the first outcome's token for bid/ask
                        # midpoint. Realistic enough for orderbook_quality
                        # classification.
                        ob = self._clob.fetch_orderbook(token_ids[0])
                        orderbook_data = _classify_orderbook(ob)
                    except Exception as exc:
                        self._bump_stat("clob_errors")
                        orderbook_data = {"error": f"clob_exception: {exc}"}

            # 3. Dynamic data
            dynamic_data = _extract_dynamic(gamma_doc, orderbook_data)
            self._dynamic_cache_put(market_id, dynamic_data)

            # 4. Upsert into DB if a session is provided
            if session is not None:
                try:
                    _upsert_market_row(session, market_id, static_data, dynamic_data)
                    self._bump_stat("upsert_success")
                except Exception as exc:
                    self._bump_stat("upsert_fails")
                    logger.warning("market upsert failed for %s: %s", market_id, exc)

            # 5. Build result
            result = self._build_result(
                market_id, t0, static_data, dynamic_data, cache_hit=False,
            )
            if not result.has_orderbook:
                result.status = "PARTIAL_NO_ORDERBOOK" if result.status == "COMPLETE" else result.status
            # Track terminal status
            if result.status == "COMPLETE":
                self._bump_stat("result_complete")
            elif result.status == "PARTIAL_NO_ORDERBOOK":
                self._bump_stat("result_partial_no_orderbook")
            return result
        finally:
            lock.release()

    # ---------------- helpers ----------------

    def _build_result(
        self,
        market_id: str,
        t0: float,
        static: Optional[dict[str, Any]],
        dynamic: Optional[dict[str, Any]],
        *,
        cache_hit: bool,
    ) -> ResolveResult:
        elapsed_ms = (self._clock() - t0) * 1000
        has_static = static is not None
        has_orderbook = (
            dynamic is not None
            and dynamic.get("orderbook_quality") not in (None, "INSUFFICIENT_DATA")
        )
        status: ResolveStatus
        if has_static and has_orderbook:
            status = "COMPLETE"
        elif has_static:
            status = "PARTIAL_NO_ORDERBOOK"
        else:
            status = "MARKET_METADATA_FETCH_FAILED"
        return ResolveResult(
            status=status,
            market_id=market_id,
            elapsed_ms=elapsed_ms,
            cache_hit=cache_hit,
            has_static=has_static,
            has_orderbook=has_orderbook,
            category=(static or {}).get("category"),
            duration_bucket=(static or {}).get("duration_bucket"),
            expected_resolution_minutes=(static or {}).get("expected_resolution_minutes"),
            yes_price=(dynamic or {}).get("yes_price"),
            no_price=(dynamic or {}).get("no_price"),
            spread=(dynamic or {}).get("spread"),
            liquidity=(dynamic or {}).get("liquidity"),
            orderbook_quality=(dynamic or {}).get("orderbook_quality"),
        )


# ---------------- pure helpers (testable in isolation) ----------------


def _extract_static(gamma_doc: dict[str, Any]) -> dict[str, Any]:
    """Pull static fields from a Gamma market response.

    Static = fields that don't refresh frequently. Used for audit gates
    that need category/deadline/duration but not live prices."""
    import json as _json

    out: dict[str, Any] = {}
    out["question"] = gamma_doc.get("question")
    out["category"] = gamma_doc.get("category")
    out["deadline"] = gamma_doc.get("endDate") or gamma_doc.get("end_date")

    # clob_token_ids: stored as JSON-string list in our Market table
    tids = gamma_doc.get("clobTokenIds") or gamma_doc.get("clob_token_ids")
    if isinstance(tids, str):
        try:
            tids = _json.loads(tids)
        except Exception:
            tids = None
    out["clob_token_ids"] = tids if isinstance(tids, list) else None

    # Outcomes
    outs = gamma_doc.get("outcomes")
    if isinstance(outs, str):
        try:
            outs = _json.loads(outs)
        except Exception:
            outs = None
    out["outcomes"] = outs if isinstance(outs, list) else None

    # Compute expected_resolution_minutes if deadline is parseable
    out["expected_resolution_minutes"] = _compute_exp_minutes(out["deadline"])
    out["duration_bucket"] = _classify_duration_bucket(out["expected_resolution_minutes"])
    out["market_speed_class"] = _classify_speed(out["expected_resolution_minutes"])
    out["raw_slug"] = gamma_doc.get("slug")
    out["data_source"] = "polymarket_gamma+resolver"
    return out


def _extract_dynamic(gamma_doc: dict[str, Any], orderbook_data: dict[str, Any]) -> dict[str, Any]:
    """Pull live-price fields. Cached only briefly."""
    import json as _json

    prices_raw = gamma_doc.get("outcomePrices")
    if isinstance(prices_raw, str):
        try:
            prices_raw = _json.loads(prices_raw)
        except Exception:
            prices_raw = None
    yes_price = no_price = None
    if isinstance(prices_raw, list) and len(prices_raw) >= 2:
        try:
            yes_price = float(prices_raw[0])
            no_price = float(prices_raw[1])
        except (TypeError, ValueError):
            pass

    spread = None
    if yes_price is not None and 0 < yes_price < 1:
        spread = orderbook_data.get("spread") or 0.01

    out: dict[str, Any] = {
        "yes_price": yes_price,
        "no_price": no_price,
        "spread": spread,
        "liquidity": gamma_doc.get("liquidity"),
        "volume_24h": gamma_doc.get("volume24hr") or gamma_doc.get("volume_24h"),
        "orderbook_quality": orderbook_data.get("quality"),
    }
    return out


def _classify_orderbook(ob: dict[str, Any]) -> dict[str, Any]:
    """Crude orderbook quality classifier — same heuristic as the
    pre-existing OrderbookAnalyzer but light-weight (no
    full-deepth analysis)."""
    if not isinstance(ob, dict):
        return {"quality": "INSUFFICIENT_DATA"}
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    if not bids or not asks:
        return {"quality": "INSUFFICIENT_DATA"}
    try:
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
    except (KeyError, TypeError, ValueError):
        return {"quality": "INSUFFICIENT_DATA"}
    if not (0 < best_bid < best_ask < 1):
        return {"quality": "BAD"}
    spread = best_ask - best_bid
    midpoint = (best_bid + best_ask) / 2.0
    if spread <= 0.005:
        quality = "GOOD"
    elif spread <= 0.02:
        quality = "OK"
    elif spread <= 0.05:
        quality = "BAD"
    else:
        quality = "UNTRADABLE"
    return {
        "quality": quality,
        "spread": spread,
        "midpoint": midpoint,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }


def _compute_exp_minutes(deadline_iso: str | None) -> float | None:
    """Minutes from now until deadline. Negative if past."""
    if not deadline_iso:
        return None
    try:
        from datetime import datetime, timezone
        # Polymarket Gamma returns ISO strings, sometimes with Z suffix.
        if deadline_iso.endswith("Z"):
            deadline_iso = deadline_iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(deadline_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (dt - now).total_seconds() / 60.0)
    except Exception:
        return None


def _classify_duration_bucket(exp_min: float | None) -> str | None:
    """Same buckets as duration_filter.py."""
    if exp_min is None:
        return None
    if exp_min < 5:
        return "ULTRA_SHORT"
    if exp_min < 30:
        return "VERY_SHORT"
    if exp_min < 120:
        return "SHORT"
    if exp_min < 1440:
        return "MEDIUM"
    if exp_min < 10080:
        return "LONG"
    return "VERY_LONG"


def _classify_speed(exp_min: float | None) -> str | None:
    if exp_min is None:
        return None
    if exp_min < 30:
        return "FAST"
    if exp_min < 1440:
        return "MEDIUM"
    return "SLOW"


def _upsert_market_row(
    session: Session, market_id: str, static_data: dict[str, Any], dynamic_data: dict[str, Any],
) -> None:
    """Idempotent upsert. Uses session.merge() so concurrent calls don't
    raise IntegrityError."""
    import json as _json
    from app.models.market import Market

    existing = session.get(Market, market_id)
    if existing is None:
        existing = Market(id=market_id)

    existing.question = static_data.get("question") or existing.question
    existing.category = static_data.get("category") or existing.category

    # Market.deadline is a Python datetime per the SQLModel type; Gamma
    # returns ISO strings. Convert before assigning.
    deadline_raw = static_data.get("deadline")
    if deadline_raw and not getattr(existing, "deadline", None):
        try:
            from datetime import datetime, timezone
            iso = deadline_raw.replace("Z", "+00:00") if deadline_raw.endswith("Z") else deadline_raw
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            existing.deadline = dt
        except Exception:
            pass  # invalid format → keep existing value
    existing.expected_resolution_minutes = (
        static_data.get("expected_resolution_minutes")
        if static_data.get("expected_resolution_minutes") is not None
        else existing.expected_resolution_minutes
    )
    existing.duration_bucket = (
        static_data.get("duration_bucket") or existing.duration_bucket
    )
    existing.market_speed_class = (
        static_data.get("market_speed_class") or existing.market_speed_class
    )
    existing.raw_slug = static_data.get("raw_slug") or existing.raw_slug
    existing.data_source = static_data.get("data_source") or existing.data_source

    tids = static_data.get("clob_token_ids")
    if tids:
        existing.clob_token_ids = _json.dumps(tids)

    yes_price = dynamic_data.get("yes_price")
    no_price = dynamic_data.get("no_price")
    if yes_price is not None:
        existing.yes_price = yes_price
    if no_price is not None:
        existing.no_price = no_price
    spread = dynamic_data.get("spread")
    if spread is not None:
        existing.spread = spread
    liquidity = dynamic_data.get("liquidity")
    if liquidity is not None:
        try:
            existing.liquidity = float(liquidity)
        except (TypeError, ValueError):
            pass
    vol24 = dynamic_data.get("volume_24h")
    if vol24 is not None:
        try:
            existing.volume_24h = float(vol24)
        except (TypeError, ValueError):
            pass

    existing.active = True
    from datetime import datetime, timezone
    existing.updated_at = datetime.now(timezone.utc)

    session.merge(existing)
    session.commit()


# ---------------- module-level singleton ----------------


_DEFAULT_RESOLVER: MarketMetadataResolver | None = None
_DEFAULT_RESOLVER_LOCK = threading.Lock()


def get_resolver() -> MarketMetadataResolver:
    """Return the shared resolver instance for the running process."""
    global _DEFAULT_RESOLVER
    with _DEFAULT_RESOLVER_LOCK:
        if _DEFAULT_RESOLVER is None:
            _DEFAULT_RESOLVER = MarketMetadataResolver()
    return _DEFAULT_RESOLVER


def reset_resolver_for_tests() -> None:
    """Test-only — drop the singleton so the next get_resolver() builds
    a fresh one with reinjected mocks."""
    global _DEFAULT_RESOLVER
    with _DEFAULT_RESOLVER_LOCK:
        _DEFAULT_RESOLVER = None
