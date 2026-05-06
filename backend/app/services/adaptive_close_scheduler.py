"""v0.7.8 Phase 2 — Adaptive close-loop scheduler.

The previous close-loop was hardcoded at 600s polling. For ULTRA_SHORT
1MN markets that resolve in <60s, this means a $5 trade locks $5 of
capital for 10 minutes — capital turnover capped at ~6 cycles/h. The
operator wants 30+ cycles/h on small capital.

This module replaces the fixed-interval polling with a per-position
adaptive schedule:

  ULTRA_SHORT (0-5min):     check every 15-30s
  VERY_SHORT  (5-30min):    check every 60-120s
  SHORT       (30-120min):  check every 5min
  MEDIUM      (120-1440min):check every 10min
  LONG / VERY_LONG:         check every 10min (current default)

Architecture:
  - Each open position has a (next_check_at, position_id) tuple
  - A min-heap (priority queue) sorted by next_check_at
  - Each cycle: pop expired entries, fetch market state, close if
    resolved (yes_price >= 0.99 OR closed=true), reschedule otherwise
  - Concurrent-safe via lock (single scheduler instance per process)

Plus Option B (reactive via existing polling):
  When the wallet polling engine fetches a market for any reason
  (a wallet just traded on it), it also passes the fresh prices to
  the scheduler, which can close immediately if any of OUR open
  positions on that market are now resolved. Saves a round-trip.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlmodel import Session, select

logger = logging.getLogger(__name__)


# Per-bucket re-check interval. The smaller the bucket, the tighter the
# check cadence — we cannot afford to lock $5 for 10 min on a 1MN trade.
BUCKET_CHECK_INTERVAL_S: dict[str, float] = {
    "ULTRA_SHORT": 20.0,    # 1-5min markets — check every 20s
    "VERY_SHORT": 60.0,     # 5-30min markets — check every 60s
    "SHORT": 300.0,         # 30-120min markets — check every 5min
    "MEDIUM": 600.0,        # 2-24h markets — check every 10min
    "LONG": 600.0,          # 1-7 day markets
    "VERY_LONG": 600.0,
    "UNKNOWN": 600.0,       # fallback when bucket missing
}

# Stale-TTL safety net: any position open longer than this is force-closed
# even if not resolved. Same as previous behaviour for safety.
STALE_TTL_HOURS: float = 24.0


@dataclass(order=True)
class _ScheduledCheck:
    """Heap entry: (next_check_at, sequence) for tie-breaking, then payload."""
    next_check_at: float
    sequence: int = field(compare=True)
    position_id: str = field(compare=False)
    market_id: str = field(compare=False)
    bucket: str = field(compare=False)


class AdaptiveCloseScheduler:
    """Adaptive scheduler — replaces the fixed 600s close-loop.

    Single-instance per backend process. Thread-safe.
    """

    def __init__(self, *, clock=time.monotonic) -> None:
        self._heap: list[_ScheduledCheck] = []
        self._known_positions: set[str] = set()
        self._lock = threading.Lock()
        self._sequence = 0
        self._clock = clock

    # ---------------- public API ----------------

    def register_position(
        self, position_id: str, market_id: str, duration_bucket: str | None,
    ) -> None:
        """Add a freshly-opened position to the schedule. Idempotent."""
        with self._lock:
            if position_id in self._known_positions:
                return
            bucket = (duration_bucket or "UNKNOWN").upper()
            interval = BUCKET_CHECK_INTERVAL_S.get(bucket, BUCKET_CHECK_INTERVAL_S["UNKNOWN"])
            self._sequence += 1
            entry = _ScheduledCheck(
                next_check_at=self._clock() + interval,
                sequence=self._sequence,
                position_id=position_id,
                market_id=market_id,
                bucket=bucket,
            )
            heapq.heappush(self._heap, entry)
            self._known_positions.add(position_id)

    def deregister_position(self, position_id: str) -> None:
        """Mark a position as closed. We don't remove the heap entry
        immediately (would require O(N) scan); instead we filter at
        pop-time via ``_known_positions``."""
        with self._lock:
            self._known_positions.discard(position_id)

    def pop_due(self, now: float | None = None, max_count: int | None = None) -> list[_ScheduledCheck]:
        """Return scheduled checks whose next_check_at is in the past.
        Stale entries (= position no longer in _known_positions) are
        silently dropped. ``max_count`` bounds returned entries so callers
        running inside a polling thread don't pay an unbounded sync HTTP
        cost when many positions are due at once (silent-stall guard)."""
        if now is None:
            now = self._clock()
        due: list[_ScheduledCheck] = []
        with self._lock:
            while self._heap and self._heap[0].next_check_at <= now:
                if max_count is not None and len(due) >= max_count:
                    break
                entry = heapq.heappop(self._heap)
                # Drop stale entries (= position closed by another path)
                if entry.position_id not in self._known_positions:
                    continue
                due.append(entry)
        return due

    def reschedule(self, entry: _ScheduledCheck) -> None:
        """Re-add a checked-but-not-resolved position to the heap."""
        with self._lock:
            if entry.position_id not in self._known_positions:
                # Position was closed during the check — don't re-add
                return
            interval = BUCKET_CHECK_INTERVAL_S.get(
                entry.bucket, BUCKET_CHECK_INTERVAL_S["UNKNOWN"]
            )
            self._sequence += 1
            new_entry = _ScheduledCheck(
                next_check_at=self._clock() + interval,
                sequence=self._sequence,
                position_id=entry.position_id,
                market_id=entry.market_id,
                bucket=entry.bucket,
            )
            heapq.heappush(self._heap, new_entry)

    def known_positions_count(self) -> int:
        with self._lock:
            return len(self._known_positions)

    def heap_size(self) -> int:
        """For diagnostics — heap may include stale entries (filtered at pop)."""
        with self._lock:
            return len(self._heap)

    # ---------------- Option B: reactive close on observed price ----------------

    def reactive_check_market(
        self, session: Session, market_id: str, yes_price: float | None,
        no_price: float | None,
    ) -> int:
        """Called by wallet_polling_engine when it fetches a market for
        any reason. If we have open positions on this market and the
        prices indicate resolution, close immediately.

        Returns the count of positions closed by this reactive path.
        """
        if yes_price is None and no_price is None:
            return 0
        # Detect resolution: any outcome at >= 0.99 or all <= 0.01
        resolved = False
        if yes_price is not None and yes_price >= 0.99:
            resolved = True
        if no_price is not None and no_price >= 0.99:
            resolved = True
        if not resolved:
            return 0

        from app.models.trade import PaperTrade
        from app.services.paper_trading_engine import _close_paper_with_reason, CLOSE_REASON_RESOLVED

        rows = list(session.exec(
            select(PaperTrade).where(
                PaperTrade.status == "open",
            ).where(
                PaperTrade.market_id == market_id,
            )
        ))
        closed_count = 0
        for trade in rows:
            outcome_upper = (trade.outcome or "").upper()
            # Determine winner side from prices
            winner = None
            if yes_price is not None and yes_price >= 0.99:
                winner = "YES"
            elif no_price is not None and no_price >= 0.99:
                winner = "NO"
            if winner is None:
                continue
            settle_price = 1.0 if outcome_upper == winner else 0.0
            _close_paper_with_reason(
                session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=settle_price,
            )
            self.deregister_position(trade.id)
            closed_count += 1
        if closed_count > 0:
            logger.info(
                "reactive_close: market=%s yes=%s no=%s closed=%d positions",
                market_id, yes_price, no_price, closed_count,
            )
        return closed_count


# ---------------- module-level singleton ----------------

_DEFAULT_SCHEDULER: AdaptiveCloseScheduler | None = None
_DEFAULT_LOCK = threading.Lock()


def get_scheduler() -> AdaptiveCloseScheduler:
    global _DEFAULT_SCHEDULER
    with _DEFAULT_LOCK:
        if _DEFAULT_SCHEDULER is None:
            _DEFAULT_SCHEDULER = AdaptiveCloseScheduler()
    return _DEFAULT_SCHEDULER


def reset_scheduler_for_tests() -> None:
    global _DEFAULT_SCHEDULER
    with _DEFAULT_LOCK:
        _DEFAULT_SCHEDULER = None


# ---------------- boot-time recovery ----------------


def boot_register_open_positions(session: Session) -> int:
    """Re-register all currently-open paper trades into the scheduler.

    Must be called once at backend startup (or first polling start) so
    that positions opened before a restart still get checked and closed.
    """
    from app.models.trade import PaperTrade
    from app.models.market import Market

    scheduler = get_scheduler()
    if scheduler.known_positions_count() > 0:
        return 0  # Already populated — skip

    open_trades = list(session.exec(
        select(PaperTrade).where(PaperTrade.status == "open")
    ))
    registered = 0
    for trade in open_trades:
        market = session.get(Market, trade.market_id) if trade.market_id else None
        bucket = getattr(market, "duration_bucket", None) or "UNKNOWN"
        scheduler.register_position(trade.id, trade.market_id or "", bucket)
        registered += 1
    if registered:
        logger.info("boot_register: re-registered %d open positions into scheduler", registered)
    return registered


# ---------------- runner: integrates with the polling loop ----------------


def run_adaptive_close_iteration(
    session: Session,
    max_entries: int | None = None,
    gamma_timeout: float | None = None,
) -> dict:
    """Single iteration of the adaptive close loop. Pop due entries,
    check market state, close resolved, reschedule pending.

    Designed to be called from the polling loop's main while-loop, NOT
    as a separate background thread. That keeps the operation
    single-process and avoids races with paper_trading_engine writes.

    ``max_entries`` bounds work per call so a sync Gamma HTTP fetch on
    each due position can't block the polling loop for minutes when many
    positions resolve simultaneously (post-trade silent-stall fix).
    ``gamma_timeout`` overrides the default 10s timeout for the hot path.
    """
    from app.models.market import Market
    from app.models.trade import PaperTrade
    from app.services.paper_trading_engine import (
        CLOSE_REASON_RESOLVED,
        _close_paper_with_reason,
    )

    scheduler = get_scheduler()
    due_entries = scheduler.pop_due(max_count=max_entries)
    if not due_entries:
        return {"checked": 0, "closed": 0, "rescheduled": 0}

    closed = 0
    rescheduled = 0
    for entry in due_entries:
        # Look up the position
        trade = session.get(PaperTrade, entry.position_id)
        if trade is None or trade.status != "open":
            scheduler.deregister_position(entry.position_id)
            continue

        # Look up the market state
        market = session.get(Market, entry.market_id) if entry.market_id else None
        if market is None:
            # No market metadata — reschedule, can't decide
            scheduler.reschedule(entry)
            rescheduled += 1
            continue

        yes_price = getattr(market, "yes_price", None)
        no_price = getattr(market, "no_price", None)
        winner = None
        if yes_price is not None and yes_price >= 0.99:
            winner = "YES"
        elif no_price is not None and no_price >= 0.99:
            winner = "NO"

        if winner is None:
            # v0.7.8 P6 B18: DB prices are stale (ingestion-time only).
            # Fetch live price from Gamma API before rescheduling.
            try:
                from app.services.polymarket.gamma_client import GammaClient
                _gc = GammaClient(timeout=gamma_timeout) if gamma_timeout is not None else GammaClient()
                _looks_cond = isinstance(entry.market_id, str) and entry.market_id.startswith("0x")
                _payload = (
                    _gc.fetch_market_by_condition(entry.market_id)
                    if _looks_cond
                    else _gc.fetch_market_details(entry.market_id)
                )
                if _payload:
                    import json as _json
                    _raw_prices = _payload.get("outcomePrices") or "[]"
                    _prices = _json.loads(_raw_prices) if isinstance(_raw_prices, str) else _raw_prices
                    _raw_outcomes = _payload.get("outcomes") or "[]"
                    _outcome_names = _json.loads(_raw_outcomes) if isinstance(_raw_outcomes, str) else _raw_outcomes
                    # Build outcome->price map (works for Yes/No, Up/Down, etc.)
                    _outcome_prices = {}
                    for _i, _o in enumerate(_outcome_names):
                        if _i < len(_prices):
                            _outcome_prices[str(_o).upper()] = float(_prices[_i])
                    # Update DB yes/no prices if available
                    if "YES" in _outcome_prices:
                        market.yes_price = _outcome_prices["YES"]
                    if "NO" in _outcome_prices:
                        market.no_price = _outcome_prices["NO"]
                    _is_closed = str(_payload.get("closed", "")).lower() == "true"
                    # Determine winner: find outcome with price >= 0.95
                    # Works for Yes/No, Up/Down, any binary outcome
                    _threshold = 0.95 if _is_closed else 0.99
                    for _oname, _oprice in _outcome_prices.items():
                        if _oprice >= _threshold:
                            winner = _oname  # "YES", "NO", "UP", "DOWN", etc.
                            break
                    session.add(market)
                    session.commit()
            except Exception as _exc:
                logger.debug("adaptive_close: live price fetch failed for %s: %s", entry.market_id, _exc)

        if winner is None:
            # Still not resolved — reschedule
            scheduler.reschedule(entry)
            rescheduled += 1
            continue

        # Settle
        outcome_upper = (trade.outcome or "").upper()
        settle_price = 1.0 if outcome_upper == winner else 0.0
        _close_paper_with_reason(
            session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=settle_price,
        )
        scheduler.deregister_position(entry.position_id)
        closed += 1

    return {
        "checked": len(due_entries),
        "closed": closed,
        "rescheduled": rescheduled,
    }


def stale_ttl_sweep(session: Session, *, ttl_hours: float = STALE_TTL_HOURS) -> dict:
    """Safety net: any position open > ttl_hours gets force-closed.

    Designed to run less frequently than the adaptive checks (e.g. once
    per hour). Same purpose as the previous `auto_close_stale_papertrades`.
    """
    from datetime import datetime, timedelta, timezone
    from app.models.trade import PaperTrade
    from app.services.paper_trading_engine import (
        CLOSE_REASON_STALE_TTL,
        _close_paper_with_reason,
    )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    rows = list(session.exec(
        select(PaperTrade)
        .where(PaperTrade.status == "open")
        .where(PaperTrade.opened_at < cutoff)
    ))
    closed_ids: list[str] = []
    scheduler = get_scheduler()
    for trade in rows:
        # Use entry price as exit (zero P&L) — we have no live price here
        exit_price = trade.average_price
        _close_paper_with_reason(
            session, trade, reason=CLOSE_REASON_STALE_TTL, exit_price=exit_price,
        )
        scheduler.deregister_position(trade.id)
        closed_ids.append(trade.id)
    return {"closed_stale_ttl": len(closed_ids), "trade_ids": closed_ids}
