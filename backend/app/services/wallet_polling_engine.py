"""v0.5.8 — Wallet polling engine.

Asynchronous, staggered, rate-limited polling of /activity per wallet for
the v0.5.7 ``allowed_aggressive`` (or ``allowed_safe``) pool. Each new
trade detected feeds the existing TradeAuditEngine → Signal → RiskEngine
→ PaperTradingEngine pipeline, identical to ``BotLoop.run_once`` but
event-driven instead of cycle-pull.

State persistence:

* Per-wallet ``last_seen_ts`` lives in
  ``data/wallet_polling_state.json`` (atomic write per wallet update).
* Global counters (running, last_poll_at, trades_detected_total, etc.)
  live on :class:`BotLoopState` so the engine can resume after process
  restart.

Lifecycle:

* :meth:`start` flips ``polling_running=True`` in the DB and creates an
  asyncio task that runs :meth:`run_loop`.
* :meth:`stop` flips ``polling_running=False`` and cancels the task.
* On FastAPI startup, ``maybe_resume`` checks the DB and restarts the
  loop if a prior run was still flagged running.

Safety:

* Rate-limited at module level via a token bucket
  (``POLLING_RATE_LIMIT_CALLS_PER_SEC``, default 12 — calibrated 2026-05-06
   from empirical test: Polymarket data-api throttles at 20/s, safe at 15/s,
   12/s leaves 20% headroom).
* Per-wallet poll interval — 2-lane:
  - ELITE: ``POLLING_INTERVAL_ELITE_SECONDS`` (default 10 s) — fast lane
    for the priority cohort. With 112 ELITE GOLD wallets / 10s = 11.2 calls/s
    (sub-ceiling).
  - non-ELITE: ``POLLING_INTERVAL_SECONDS`` (default 30 s) — slow lane.
  Stagger uniform across the pool to avoid spikes beyond
  ``cohort_size / interval`` requests/s.
* Hard refusal: if ``LIVE_ENABLED=true`` is observed at start time, the
  engine raises immediately. Paper-only path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import httpx
from sqlmodel import Session, select

from app.config import get_settings
from app.database import engine
from app.models.bot import BotLoopState
from app.services.audit_logger import AuditLogger
from app.services.paper_trading_engine import PaperTradingEngine
from app.services.risk_engine import RiskEngine
from app.services.signal_engine import SignalEngine
from app.services.trade_audit_engine import TradeAuditEngine
from app.services.validated_paper_universe import ValidatedPaperUniverse

logger = logging.getLogger(__name__)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


POLLING_INTERVAL_SECONDS = env_int("POLLING_INTERVAL_SECONDS", 30)
# v0.7.8 — ELITE wallets get a tighter re-poll interval so 1MN/2MN
# markets stay copy-tradable. With cohort 981 + rate 5/s the effective
# full-cycle is ~196s; that means a wallet trade on a 60s market is
# already resolved before our next poll. ELITE @ 30s + non-ELITE @ 90s
# gives total ~4.5 calls/s (within rate limit) and fresh ELITE every
# 30s — enough margin for 1MN markets.
POLLING_INTERVAL_ELITE_SECONDS = env_int("POLLING_INTERVAL_ELITE_SECONDS", 10)
POLLING_RATE_LIMIT_CALLS_PER_SEC = env_float("POLLING_RATE_LIMIT_CALLS_PER_SEC", 12.0)

# 2026-05-18 — Hot lane scheduler (HOT 10s / WARM 60s / COLD 120s).
# Feature flag : when enabled, polling priorities wallets by recent activity
# (paper trade + signal + fresh API) instead of binary ELITE/non-ELITE.
# Cache classification refreshed every 5min via background task.
# Cap HOT lane at top-N by score to keep budget under rate limit.
# Falls back to uniform polling if classifier raises exception.
SCHEDULER_HOT_LANE_ENABLED = env_bool("SCHEDULER_HOT_LANE_ENABLED", False)
HOT_LANE_HOT_INTERVAL_S = env_float("HOT_LANE_HOT_INTERVAL_S", 10.0)
HOT_LANE_WARM_INTERVAL_S = env_float("HOT_LANE_WARM_INTERVAL_S", 60.0)
HOT_LANE_COLD_INTERVAL_S = env_float("HOT_LANE_COLD_INTERVAL_S", 120.0)
HOT_LANE_MAX_HOT_COUNT = env_int("HOT_LANE_MAX_HOT_COUNT", 40)
HOT_LANE_REFRESH_INTERVAL_S = env_int("HOT_LANE_REFRESH_INTERVAL_S", 300)  # 5min
POLLING_FETCH_LIMIT = env_int("POLLING_FETCH_LIMIT", 50)
# v0.7.2 B2 — Restart freshness clamp (default 30 min). When the
# polling resumes after a pause, ``last_seen_ts`` may be hours old. We
# refuse to re-ingest trades older than this floor so the audit
# pipeline only sees copy-tradable signals (not 11h-old fossils).
POLLING_RESTART_FRESHNESS_S = env_int("POLLING_RESTART_FRESHNESS_S", 1800)
# Alias (preserves audit data — backfill ingest still happens) :
BACKFILL_LOOKBACK_S = POLLING_RESTART_FRESHNESS_S

# v0.7.8 P3.1 — paper-eligibility freshness gate. Audits run on all
# trades within BACKFILL_LOOKBACK_S, but paper trades only open on
# trades younger than these thresholds. Decoupling audit lookback from
# paper eligibility keeps the data signal AND blocks stale paper opens.
PAPER_ELIGIBLE_MAX_AGE_S = env_int("PAPER_ELIGIBLE_MAX_AGE_S", 300)
# Crypto 5-min markets are extra-time-sensitive: copy_delay > 90s on a
# market that resolves in 300s = essentially copying after edge is gone.
PAPER_ELIGIBLE_MAX_AGE_CRYPTO_5MIN_S = env_int("PAPER_ELIGIBLE_MAX_AGE_CRYPTO_5MIN_S", 90)

# Polling-source labels. Stored on the audit context and surfaced in
# notradedecision.details so reports can split fresh / backfill cadence.
POLLING_SOURCE_FRESH = "FRESH_POLLING"
POLLING_SOURCE_RESTART_BACKFILL = "RESTART_BACKFILL"
POLLING_SOURCE_STALE_BACKFILL = "STALE_BACKFILL"


def classify_polling_source(
    *,
    traded_at_unix: int | float,
    now_unix: int | float,
    is_crypto_5min: bool = False,
) -> tuple[str, int]:
    """Classify a freshly-observed wallet trade into one of the three
    source labels and return ``(source, poll_lag_seconds)``.

    - FRESH_POLLING       : poll_lag <= paper-eligible threshold
    - RESTART_BACKFILL    : threshold < poll_lag <= BACKFILL_LOOKBACK_S
    - STALE_BACKFILL      : poll_lag > BACKFILL_LOOKBACK_S (clamp leak)

    Crypto-5min markets get the stricter 90s threshold by default.
    """
    poll_lag = max(0, int(now_unix) - int(traded_at_unix))
    threshold = (
        PAPER_ELIGIBLE_MAX_AGE_CRYPTO_5MIN_S
        if is_crypto_5min
        else PAPER_ELIGIBLE_MAX_AGE_S
    )
    if poll_lag <= threshold:
        return POLLING_SOURCE_FRESH, poll_lag
    if poll_lag <= BACKFILL_LOOKBACK_S:
        return POLLING_SOURCE_RESTART_BACKFILL, poll_lag
    return POLLING_SOURCE_STALE_BACKFILL, poll_lag
POLLING_STATE_FILE = Path(os.environ.get("POLLING_STATE_FILE") or "../data/wallet_polling_state.json")
DATA_API_BASE = os.environ.get("DATA_API_BASE_URL") or "https://data-api.polymarket.com"
HTTP_TIMEOUT = env_float("POLLING_HTTP_TIMEOUT_S", 15.0)


@dataclass
class PollingState:
    """In-memory snapshot of the per-wallet ``last_seen_ts`` (unix seconds).

    P0-B 2026-05-18 : update/save are guarded by a re-entrant lock so the
    multi-worker pool can call ``update`` concurrently without dict races
    or interleaved JSON writes. The lock is a threading.RLock because
    update is called from sync code (via ``asyncio.to_thread``) on
    multiple worker threads.
    """

    last_seen_ts: dict[str, int] = field(default_factory=dict)
    state_file: Path = POLLING_STATE_FILE
    _lock: "threading.RLock" = field(default_factory=lambda: __import__("threading").RLock())

    @classmethod
    def load(cls, path: Path = POLLING_STATE_FILE) -> "PollingState":
        if not path.exists():
            return cls(last_seen_ts={}, state_file=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(last_seen_ts={k.lower(): int(v) for k, v in data.items()}, state_file=path)
        except Exception as exc:
            logger.warning("polling state load failed: %s", exc)
            return cls(last_seen_ts={}, state_file=path)

    def save(self) -> None:
        with self._lock:
            tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp.write_text(json.dumps(self.last_seen_ts, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.state_file)

    def update(self, address: str, ts: int) -> None:
        addr = address.lower()
        with self._lock:
            prev = self.last_seen_ts.get(addr, 0)
            if ts > prev:
                self.last_seen_ts[addr] = ts
                self.save()


class TokenBucket:
    """Simple async-friendly token bucket for global rate limiting."""

    def __init__(self, rate_per_sec: float, capacity: int | None = None) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else max(1, int(rate_per_sec * 2))
        self.tokens = float(self.capacity)
        self.last_refill = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_event_loop().time()
                elapsed = now - self.last_refill
                self.tokens = min(float(self.capacity), self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            await asyncio.sleep(wait)


class WalletPollingEngine:
    """Singleton-style controller for the polling task. Designed to be
    instantiated once and started/stopped through its public methods.
    """

    _instance: "WalletPollingEngine | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self.settings = get_settings()
        if self.settings.live_enabled:
            raise PermissionError(
                "WalletPollingEngine refuses to start with LIVE_ENABLED=true"
            )
        self.state = PollingState.load()
        self.bucket = TokenBucket(POLLING_RATE_LIMIT_CALLS_PER_SEC)
        self._task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None
        # Silent-stall watchdog — `_iteration_heartbeat` is updated at the
        # top of every run_loop iteration (in-memory, no DB write so it
        # can't be blocked by SQLite locks). The `_watchdog_task` checks
        # this every WATCHDOG_TICK_S and force-cancels run_loop if stale.
        self._iteration_heartbeat: float = 0.0
        self._watchdog_task: asyncio.Task[Any] | None = None
        self._cohort: list[str] = []
        # v0.7.8 — ELITE addresses get the tighter ELITE-interval re-poll
        # so we don't miss 1MN/2MN market signals. Populated by
        # load_cohort and consulted in run_loop when scheduling next_due.
        self._elite_addresses: set[str] = set()
        self._trades_detected_session = 0
        self._paper_trades_session = 0
        self._errors_session = 0
        self._polling_start_unix: int | None = None
        self._poll_count = 0
        # v0.7.7 B9.1 — track last background close-loop fire so we can
        # invoke it periodically from the polling loop without spawning a
        # second async task.
        self._last_close_loop_unix: int = 0
        self._close_loop_total_closed: int = 0

    @classmethod
    def instance(cls) -> "WalletPollingEngine":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ---------------- cohort ----------------

    @staticmethod
    def load_cohort(
        path: Path | None = None,
        *,
        session: "Session | None" = None,
        capital_total: float | None = None,
    ) -> list[str]:
        """Read the truth pool and return wallets ``allowed_aggressive=True``.

        v0.7.8 P6 LOCKED 2026-05-05 — capital-tier aware:
        - At SMALL (<$500): only ELITE traded.
        - At MEDIUM/LARGE: ELITE+STRONG traded.
        - At HUGE (≥$50k): only ELITE traded (institutional preservation).

        v0.7.8 P6 — priority ORDER BY computed-at-query:
        1. ELITE bucket bonus (always first)
        2. Winrate bucket: GOLD 99-100% > 95-99% > 90-95%
        3. recent_activity_score (descending)
        4. log10(W+L) sample size bonus
        5. Stale penalty: -100 if recent_activity < 25

        If ``capital_total`` is None, falls back to ELITE+STRONG (back-compat
        for tests/replay). Production calls pass capital_total from BotState.
        """
        import csv

        if path is None:
            u = ValidatedPaperUniverse()
            for candidate_name in (
                "validated_paper_universe_latest.csv",
                "validated_paper_universe_v0_7_0.csv",
                "validated_paper_universe_v0_5_8.csv",
                "validated_paper_universe_v0_5_7.csv",
                "validated_paper_universe_v0_5_6.csv",
            ):
                candidate_path = u.exports_dir / candidate_name
                if candidate_path.exists():
                    path = candidate_path
                    break
        if path is None or not path.exists():
            logger.warning("polling cohort: no universe csv found — empty pool")
            return []
        cohort_csv: list[str] = []
        with path.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                allowed = (r.get("allowed_aggressive") or "").strip().lower()
                if allowed in ("true", "1", "yes"):
                    addr = (r.get("address") or "").lower()
                    if addr:
                        cohort_csv.append(addr)

        # v0.7.8 P6 — capital-tier aware filter + priority ORDER BY.
        if session is None:
            return cohort_csv  # back-compat: no session → no filter or priority

        try:
            from app.services.capital_allocator import (
                _resolve_tier,
                classify_wr_bucket,
                is_wallet_allowed_at_tier,
            )
            from app.models.wallet import MarketFirstWalletRecord
            from sqlmodel import select as _select
            import math

            # Determine allowed candidate_statuses by capital tier.
            if capital_total is None:
                allowed_statuses = ["ELITE", "STRONG"]
                tier_name = "ANY"
                _bucket_filter_active = False
            else:
                rule = _resolve_tier(float(capital_total))
                allowed_set = rule.get("allowed_tiers_intersect")
                allowed_statuses = sorted(allowed_set) if allowed_set else ["ELITE", "STRONG"]
                tier_name = rule["name"]
                _bucket_filter_active = True

            # Fetch records, then sort in Python (avoids SQL alias issues).
            rows = session.exec(
                _select(MarketFirstWalletRecord).where(
                    MarketFirstWalletRecord.candidate_status.in_(allowed_statuses)
                )
            ).all()

            # v0.7.8 P6 — 12-tier refactor 2026-05-06: also filter by WR bucket.
            # NANO/TINY/MICRO accept ELITE GOLD only; SMALL+ adds SILVER; ≥$10k
            # adds BRONZE. STRONG only when GOLD bucket and tier ≥ MEDIUM.
            if _bucket_filter_active:
                rows = [
                    r for r in rows
                    if is_wallet_allowed_at_tier(
                        r.candidate_status,
                        r.resolved_market_win_rate,
                        float(capital_total),
                    )
                ]

            def _priority(r: MarketFirstWalletRecord) -> float:
                """Computed-at-query priority — higher = polled first.
                ELITE bonus + WR bucket + activity - stale_penalty."""
                p = 0.0
                if (r.candidate_status or "") == "ELITE":
                    p += 1000.0
                wr = float(r.resolved_market_win_rate or 0)
                if wr >= 0.99:
                    p += 500.0
                elif wr >= 0.95:
                    p += 300.0
                elif wr >= 0.90:
                    p += 100.0
                act = float(r.recent_activity_score or 0)
                p += act
                if act < 25:
                    p -= 100.0
                return p

            # Sort by priority descending; tie-breaker: wr desc, sample desc.
            sorted_rows = sorted(
                rows,
                key=lambda r: (
                    _priority(r),
                    float(r.resolved_market_win_rate or 0),
                    int((r.resolved_winning_markets or 0) + (r.resolved_losing_markets or 0)),
                ),
                reverse=True,
            )
            tradable_ordered = [(r.address or "").lower() for r in sorted_rows if r.address]

            # If MFWR has no matching rows (fresh DB / test fixture), fall
            # back to the CSV-derived cohort intact rather than returning
            # an empty list. This matches the original v0.7.2 B5 behavior.
            if not tradable_ordered:
                logger.warning(
                    "polling cohort: MFWR empty for allowed_statuses=%s — keeping CSV cohort %d",
                    allowed_statuses, len(cohort_csv),
                )
                return cohort_csv

            # 2026-05-16 P6 fix : MFWR = source of truth runtime.
            # AVANT : cohort = MFWR ∩ CSV → bug, toutes nouvelles promotions
            # invisibles polling tant que CSV pas régénéré (CSV legacy v0.5.4).
            # APRÈS : cohort = MFWR (filtré par tier + sorted by priority).
            # CSV ne sert plus que de fallback si MFWR vide (ligne 391+).
            cohort = tradable_ordered
            csv_set = {a.lower() for a in cohort_csv}
            n_only_mfwr = sum(1 for a in cohort if a not in csv_set)
            n_csv_orphan = len(csv_set - set(cohort))
            logger.info(
                "polling cohort (P6 MFWR-first): mfwr=%d "
                "(new_not_in_csv=%d csv_orphans=%d) csv=%d tier=%s allowed=%s",
                len(cohort), n_only_mfwr, n_csv_orphan,
                len(cohort_csv), tier_name, allowed_statuses,
            )
            return cohort
        except Exception as exc:  # pragma: no cover — DB unavailable in tests
            logger.warning("polling cohort: MFWR P6 filter skipped: %s", exc)
            return cohort_csv

    @staticmethod
    def load_elite_set(session: "Session | None") -> set[str]:
        """v0.7.8 — return the lowercase set of MFWR ELITE addresses.

        run_loop calls this alongside ``load_cohort`` to know which
        wallets earn the tighter ELITE re-poll interval. Returns ``set()``
        on any failure (DB unavailable, schema mismatch) so the polling
        loop falls back to a uniform interval rather than crashing."""
        if session is None:
            return set()
        try:
            from sqlmodel import select as _select
            from app.models.wallet import MarketFirstWalletRecord
            rows = session.exec(
                _select(MarketFirstWalletRecord.address).where(
                    MarketFirstWalletRecord.candidate_status == "ELITE"
                )
            ).all()
            out: set[str] = set()
            for row in rows:
                addr = row[0] if isinstance(row, tuple) else (row if isinstance(row, str) else getattr(row, "address", None))
                if addr:
                    out.add(addr.lower())
            return out
        except Exception as exc:  # pragma: no cover
            logger.warning("polling: load_elite_set failed: %s", exc)
            return set()

    # ---------------- network ----------------

    async def fetch_recent_activity(
        self,
        client: httpx.AsyncClient,
        address: str,
        since_ts: int,
    ) -> list[dict[str, Any]]:
        """Fetch the wallet's most recent activity. Filters to ``ts > since_ts``."""
        await self.bucket.acquire()
        try:
            r = await client.get(
                f"{DATA_API_BASE}/activity",
                params={"user": address, "limit": POLLING_FETCH_LIMIT, "offset": 0},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code != 200:
                return []
            rows = r.json()
            if not isinstance(rows, list):
                return []
        except Exception as exc:
            logger.warning(
                "polling_err site=fetch_user_trades type=%s wallet=%s err=%r",
                type(exc).__name__, address, str(exc),
            )
            self._errors_session += 1
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                ts = int(row.get("timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if ts <= since_ts:
                continue
            side = (row.get("side") or "").upper()
            if side not in {"BUY", "SELL"}:
                continue
            out.append(row)
        return out

    # ---------------- pipeline ----------------

    def _process_trade_in_session(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Run the full audit→signal→risk→paper pipeline for one trade.
        Always opens its own DB session — must NOT reuse FastAPI's request
        session from the polling coroutine.

        v0.7.4 B8-A — direct audit_id lookup (was: limit=10 scan that
        silently dropped re-processed trades whose audit was past the
        top-10 by audited_at).

        v0.7.4 B8-B — when audit.decision != PAPER_TRADE, write a
        NoTradeDecision row (reason_code='AUDIT_DECISION_NOT_PAPER')
        instead of silently breaking. Guarantees that no detected
        trade disappears from observability.
        """
        from uuid import uuid4 as _uuid4
        from app.models.trade import NoTradeDecision, TradeAuditRecord
        with Session(engine) as session:
            try:
                # v0.7.8 C1 — ensure market metadata is in the local DB
                # BEFORE the audit decision runs. Without this, missing-
                # market signals produce ``liquidity_score=0`` +
                # ``orderbook_quality=INSUFFICIENT_DATA`` and the audit
                # downgrades to WATCH on a fake LOW_LIQUIDITY signal.
                # Latency-safe: per-resolver token bucket + 1.5s deadline,
                # so a slow Gamma fetch can't stall the polling loop.
                _market_id = (raw or {}).get("market_id") or (raw or {}).get("conditionId")
                if _market_id:
                    try:
                        from app.services.market_metadata_resolver import get_resolver
                        from app.models.market import Market as _Market
                        if session.get(_Market, _market_id) is None:
                            _resolver = get_resolver()
                            _res = _resolver.resolve(_market_id, session=session)
                            if _res.status in (
                                "MARKET_METADATA_NOT_FOUND",
                                "MARKET_METADATA_FETCH_FAILED",
                                "MARKET_METADATA_TIMEOUT",
                            ):
                                no_trade_payload = json.dumps({
                                    "market_id": _market_id,
                                    "resolver_status": _res.status,
                                    "resolver_elapsed_ms": _res.elapsed_ms,
                                    "resolver_error": _res.error,
                                })
                                no_trade = NoTradeDecision(
                                    id=str(_uuid4()),
                                    market_id=_market_id,
                                    wallet_address=(raw or {}).get("wallet_address"),
                                    reason_code=_res.status,
                                    details=no_trade_payload,
                                )
                                session.add(no_trade)
                                session.commit()
                                return {
                                    "executed": False,
                                    "rejected": True,
                                    "reason": _res.status,
                                }
                    except Exception as _resolver_exc:
                        logger.warning(
                            "market metadata resolver hook failed (market=%s): %s",
                            _market_id, _resolver_exc,
                        )
                audit_engine = TradeAuditEngine(session)
                # v0.7.8 Phase 6 — instrument the audit step
                from app.services.latency_tracker import track_latency
                with track_latency("audit_trade"):
                    trade_audit_result = audit_engine.audit_trade(raw)
                # audit_trade saves PublicTrade + TradeAuditRecord (merge upserts).
                signal_engine = SignalEngine(session)
                paper_engine = PaperTradingEngine(session)
                expected_audit_id = f"audit-{trade_audit_result.trade_id}"

                # B8-A: direct lookup — no more limit=10 silent-drop window.
                audit_record = session.get(TradeAuditRecord, expected_audit_id)
                if audit_record is None:
                    logger.error(
                        "polling_err site=audit_missing_after_persist trade_id=%s — "
                        "DB sync race or commit not landed.",
                        trade_audit_result.trade_id,
                    )
                    self._errors_session += 1
                    return {
                        "audit_id": expected_audit_id,
                        "executed": False,
                        "rejected": True,
                        "reason": "AUDIT_MISSING_AFTER_PERSIST",
                    }
                # P0-B 2026-05-18 log-only observability — annotate active obs.
                from app.services import polling_delay_observer as _pdo
                _pdo.attach_audit_active(audit_record)
                signal = signal_engine.build_signal_from_trade_audit(audit_record)

                # v0.7.8 P3.1 — paper-eligibility freshness gate.
                # Fires BEFORE the decision gate so we don't mask stale
                # backfill behind audit-decision noise. Audit row above
                # is preserved (data signal kept).
                #
                # v0.7.8 P3.1 BUGFIX — use audit_record.copy_delay_seconds
                # as the canonical poll_lag. The previous version read
                # ``raw["timestamp"]`` which is NOT in the normalized
                # dict (only "traded_at" ISO is) so poll_lag was always
                # 0 and the gate never fired.
                from app.models.market import Market as _Market
                _market_row = session.get(_Market, audit_record.market_id) if audit_record.market_id else None
                _exp_min = getattr(_market_row, "expected_resolution_minutes", None) if _market_row else None
                _is_crypto_5min = bool(
                    _market_row is not None
                    and (_exp_min is not None and 0 < _exp_min <= 10)
                    and (_market_row.category or "").strip().lower() == "crypto"
                )
                _poll_lag_s = int(audit_record.copy_delay_seconds or 0)
                _threshold = (
                    PAPER_ELIGIBLE_MAX_AGE_CRYPTO_5MIN_S
                    if _is_crypto_5min
                    else PAPER_ELIGIBLE_MAX_AGE_S
                )
                if _poll_lag_s <= _threshold:
                    _polling_source = POLLING_SOURCE_FRESH
                elif _poll_lag_s <= BACKFILL_LOOKBACK_S:
                    _polling_source = POLLING_SOURCE_RESTART_BACKFILL
                else:
                    _polling_source = POLLING_SOURCE_STALE_BACKFILL

                if _polling_source != POLLING_SOURCE_FRESH:
                    no_trade_payload = json.dumps({
                        "audit_id": expected_audit_id,
                        "polling_source": _polling_source,
                        "poll_lag_seconds": _poll_lag_s,
                        "is_crypto_5min": _is_crypto_5min,
                        "expected_resolution_minutes": _exp_min,
                        "category": (_market_row.category if _market_row else None),
                        "paper_eligible_threshold_s": (
                            PAPER_ELIGIBLE_MAX_AGE_CRYPTO_5MIN_S if _is_crypto_5min
                            else PAPER_ELIGIBLE_MAX_AGE_S
                        ),
                        "audit_decision": audit_record.decision,
                    })
                    no_trade = NoTradeDecision(
                        id=str(_uuid4()),
                        signal_id=signal.id,
                        market_id=audit_record.market_id,
                        wallet_address=audit_record.wallet_address,
                        reason_code="STALE_SIGNAL_BACKFILL",
                        details=no_trade_payload,
                    )
                    session.add(no_trade)
                    session.commit()
                    return {
                        "audit_id": expected_audit_id,
                        "executed": False,
                        "rejected": True,
                        "reason": f"STALE_SIGNAL_BACKFILL:{_polling_source}:{_poll_lag_s}s",
                    }

                # B8-B: log non-PAPER decisions explicitly so they don't
                # disappear from the observability surface.
                if signal.decision != "PAPER_TRADE":
                    # 2026-05-14 CLOB retry queue hook (Round 9 review fix).
                    # Si rejet uniquement causé par orderbook BAD/UNTRADABLE
                    # sur un audit qui pass tous les autres gates strict (ELITE
                    # + strong edge + tq>=70 + liq>0), au lieu de rejeter
                    # définitivement on enqueue dans PendingClobRetry. Le
                    # worker re-test CLOB toutes les 10-180s, ouvre paper SI
                    # tous les strict gates passent au retry (sinon expire).
                    # Aucun seuil baissé.
                    try:
                        from app.services.clob_retry_service import ClobRetryService
                        _retry_svc = ClobRetryService(session)
                        _queued_id = _retry_svc.maybe_queue(
                            audit_id=expected_audit_id,
                            wallet_address=audit_record.wallet_address,
                            market_id=audit_record.market_id,
                            outcome=audit_record.outcome,
                            side=audit_record.side,
                            raw_signal_price=audit_record.price,
                            notional_usd=audit_record.notional_usd,
                            copyable_edge=audit_record.copyable_edge,
                            trade_quality_score=audit_record.trade_quality_score,
                            wallet_tier=audit_record.wallet_tier,
                            orderbook_quality=audit_record.orderbook_quality,
                            market_liquidity_score=audit_record.market_liquidity_score,
                        )
                    except Exception as _qexc:
                        logger.warning(
                            "clob_retry maybe_queue failed for audit %s: %s",
                            expected_audit_id, _qexc,
                        )
                        _queued_id = None

                    no_trade_payload = json.dumps({
                        "audit_id": expected_audit_id,
                        "audit_decision": audit_record.decision,
                        "wallet_score": audit_record.wallet_score,
                        "wallet_tier": audit_record.wallet_tier,
                        "copyable_edge": audit_record.copyable_edge,
                        "trade_quality_score": audit_record.trade_quality_score,
                        "outcome": audit_record.outcome,
                        "side": audit_record.side,
                        "clob_retry_id": _queued_id,
                    })
                    no_trade = NoTradeDecision(
                        id=str(_uuid4()),
                        signal_id=signal.id,
                        market_id=audit_record.market_id,
                        wallet_address=audit_record.wallet_address,
                        reason_code="AUDIT_DECISION_NOT_PAPER",
                        details=no_trade_payload,
                    )
                    session.add(no_trade)
                    session.commit()
                    return {
                        "audit_id": expected_audit_id,
                        "executed": False,
                        "rejected": True,
                        "reason": f"AUDIT_DECISION_NOT_PAPER:{signal.decision}",
                    }

                # PAPER_TRADE path — through cluster engine + allocator.
                context = self._build_paper_context(session, signal, trade_audit_result)
                result = paper_engine.maybe_auto_trade(signal, context)
                executed = bool(result.get("executed"))
                reason = "EXECUTED" if executed else str(result.get("reason_code") or "REJECTED")
                if executed:
                    self._paper_trades_session += 1
                    logger.info(
                        "PAPER TRADE OPENED: audit=%s market=%s wallet=%s",
                        expected_audit_id, audit_record.market_id,
                        audit_record.wallet_address,
                    )
                elif reason not in ("EXECUTED",):
                    # Log silent rejections from maybe_auto_trade that don't
                    # write their own NoTradeDecision (early returns in lines
                    # 1010-1022: duplicate signal/market, mode check, etc.)
                    logger.info(
                        "PAPER_TRADE rejected by maybe_auto_trade: %s (audit=%s)",
                        reason, expected_audit_id,
                    )
                return {
                    "audit_id": expected_audit_id,
                    "executed": executed,
                    "rejected": not executed,
                    "reason": reason,
                }
            except Exception as exc:
                logger.warning(
                    "polling_err site=pipeline_one_trade type=%s err=%r",
                    type(exc).__name__, str(exc),
                )
                self._errors_session += 1
                return {"audit_id": None, "executed": False, "rejected": True, "reason": f"EXCEPTION:{exc}"}

    def _build_paper_context(
        self,
        session: Session,
        signal: Any,
        audit_result: Any,
    ) -> dict[str, Any]:
        """Mirror BotLoop._build_audit_context for the polling path."""
        from app.models.market import Market
        from app.models.wallet import MarketFirstWalletRecord

        market = session.get(Market, signal.market_id) if signal.market_id else None
        market_liquidity_usd = float(market.liquidity) if market and market.liquidity else 0.0
        wallet_addr = (audit_result.wallet or "").lower()
        wallet_record = (
            session.get(MarketFirstWalletRecord, wallet_addr) if wallet_addr else None
        )
        candidate_status = wallet_record.candidate_status if wallet_record else None
        win_rate_confidence = wallet_record.win_rate_confidence if wallet_record else "INSUFFICIENT_DATA"
        market_sample_size = wallet_record.resolved_markets_traded if wallet_record else 0
        return {
            "spread_pct": audit_result.estimated_spread or 0.0,
            "liquidity": market_liquidity_usd,
            "copy_delay_seconds": audit_result.copy_delay_seconds or 0.0,
            "price_deterioration": audit_result.price_deterioration or 0.0,
            "confidence_score": signal.confidence * 100,
            "copyable_edge_score": min(100.0, (audit_result.copyable_edge or 0.0) * 200),
            "wallet_tier": audit_result.wallet_tier,
            "candidate_status": candidate_status,
            "win_rate_confidence": win_rate_confidence,
            "market_sample_size": market_sample_size,
            "orderbook_quality": audit_result.orderbook_quality,
            "liquidity_score": audit_result.market_liquidity_score or 0.0,
            "price": audit_result.price or 0.5,
            "side": (audit_result.side or "BUY").upper(),
            "wallet": audit_result.wallet,
            "signal_score": signal.score,
            "signal_id": signal.id,
            "market_id": signal.market_id,
        }

    # ---------------- per-wallet poll ----------------

    async def poll_wallet_once(self, client: httpx.AsyncClient, address: str) -> int:
        """One scheduled poll for a single wallet. Returns the count of new
        trades that triggered the pipeline.

        First-contact handling: when ``last_seen_ts`` is 0 we anchor
        ``since_ts`` to ``polling_started_at - 60s`` (a constant for the
        whole run) so every wallet's first poll catches trades that
        happened from the moment polling started, regardless of when in
        the cycle the wallet itself gets first-polled. Without this, the
        790th-polled wallet's first since_ts would be 20+ minutes after
        polling started and miss every trade of the early cohort wave.

        v0.7.2 B2 — restart freshness clamp. ``last_seen_ts`` persists
        across polling sessions on disk. After a long pause, fetching
        from a stale ``last_seen_ts`` backfills hours of historical
        trades that are too old to be copy-tradable. We clamp
        ``since_ts`` so we never look back more than
        ``POLLING_RESTART_FRESHNESS_S`` seconds (default 30 minutes).
        """
        now_unix = int(datetime.now(UTC).timestamp())
        freshness_floor = now_unix - POLLING_RESTART_FRESHNESS_S
        since_ts = self.state.last_seen_ts.get(address, 0)
        first_contact = since_ts == 0
        if first_contact:
            anchor = self._polling_start_unix or (now_unix - 60)
            since_ts = max(freshness_floor, anchor - 60)
        elif since_ts < freshness_floor:
            # Stale last_seen_ts (long pause since previous session) —
            # don't re-ingest historical trades older than the freshness
            # floor. Logged once per wallet for traceability.
            logger.info(
                "polling restart clamp: wallet=%s stale_since_ts=%d -> freshness_floor=%d",
                address, since_ts, freshness_floor,
            )
            since_ts = freshness_floor
        rows = await self.fetch_recent_activity(client, address, since_ts)
        if not rows:
            if first_contact:
                # Plant the marker so future polls only fetch trades newer
                # than the moment we first observed this wallet.
                self.state.update(address, since_ts)
            return 0
        rows.sort(key=lambda r: int(r.get("timestamp") or 0))
        new_trades = 0
        for raw in rows:
            ts = int(raw.get("timestamp") or 0)
            normalized = self._raw_to_audit_input(raw, address)
            # P0-B 2026-05-18 — log-only delay observer (no-op if flag off).
            # Pass the RAW Polymarket dict (has `timestamp`, `proxyWallet`) for
            # source_trade_ts capture; the normalized dict drops `timestamp`.
            from app.services import polling_delay_observer as _pdo
            _obs = _pdo.start_observation({**raw, "wallet_address": address})
            result = None
            try:
                result = await asyncio.to_thread(self._process_trade_in_session, normalized)
            finally:
                _pdo.finalize_from_result(_obs, result)
            new_trades += 1
            self._trades_detected_session += 1
            if result and result.get("executed"):
                self._paper_trades_session += 1
            self.state.update(address, ts)
        if first_contact and new_trades == 0:
            self.state.update(address, since_ts)
        return new_trades

    @staticmethod
    def _raw_to_audit_input(raw: dict[str, Any], address: str) -> dict[str, Any]:
        return {
            "trade_id": raw.get("transactionHash") or raw.get("hash") or raw.get("id"),
            "address": address,
            "wallet": address,
            "user": address,
            "market_id": raw.get("conditionId") or raw.get("market"),
            "market": raw.get("conditionId") or raw.get("market"),
            "outcome": raw.get("outcome") or "",
            "side": (raw.get("side") or "").upper(),
            "price": raw.get("price"),
            "size": raw.get("size"),
            "notional_usd": raw.get("usdcSize") or (
                float(raw.get("price") or 0) * float(raw.get("size") or 0)
            ),
            "traded_at": datetime.fromtimestamp(int(raw.get("timestamp") or 0), UTC).isoformat(),
            "data_source": "polling",
        }

    # ---------------- main loop ----------------

    def _classify_cohort_lanes(self, addresses: list) -> dict:
        """Classify all wallets via BATCH queries (perf-critical).

        2026-05-18 v2 REFACTOR : remplace per-wallet 4 SELECT (2988 queries
        sur 747 wallets = 6min freeze) par **3 batch GROUP BY queries** :
            1. papertrade : MAX(opened_at) WHERE opened_at >= -24h GROUP BY wallet
            2. notradedecision : MAX(created_at) WHERE created_at >= -24h GROUP BY wallet
            3. marketfirstwalletrecord : recent_activity_score (1 SELECT)

        Puis classify in-memory. Target: <1s pour 747 wallets.

        Safe : falls back to all WARM si classifier raises. Caps HOT à
        HOT_LANE_MAX_HOT_COUNT (top-N by score).
        """
        try:
            from app.services.hot_lane_classifier import (
                WalletActivitySnapshot, LANE_HOT, LANE_WARM, LANE_COLD,
            )
            from sqlmodel import Session, text
            from app.database import engine as _engine
        except Exception as exc:
            logger.warning("hot_lane_classifier import failed: %r — fallback WARM", exc)
            return {a: "WARM" for a in addresses}

        import time as _time
        t0 = _time.time()
        try:
            with Session(_engine) as session:
                addr_set = set(addresses)
                # BATCH 1 : derniers paper trades par wallet (last 24h)
                paper_rows = session.exec(text(
                    "SELECT wallet_address, MAX(opened_at) AS last_open "
                    "FROM papertrade WHERE opened_at >= datetime('now', '-24 hours') "
                    "GROUP BY wallet_address"
                )).all()
                paper_last: dict[str, str] = {r[0]: r[1] for r in paper_rows if r[0] in addr_set}

                # BATCH 2 : derniers signaux par wallet (last 24h)
                signal_rows = session.exec(text(
                    "SELECT wallet_address, MAX(created_at) AS last_signal "
                    "FROM notradedecision WHERE created_at >= datetime('now', '-24 hours') "
                    "AND wallet_address IS NOT NULL "
                    "GROUP BY wallet_address"
                )).all()
                signal_last: dict[str, str] = {r[0]: r[1] for r in signal_rows if r[0] in addr_set}

                # BATCH 3 : recent_activity_score par wallet (MFWR)
                ras_rows = session.exec(text(
                    "SELECT address, recent_activity_score FROM marketfirstwalletrecord"
                )).all()
                ras_map: dict[str, float] = {
                    r[0]: float(r[1]) if r[1] is not None else 0.0
                    for r in ras_rows if r[0] in addr_set
                }

                # Compute cutoffs strings (SQLite format)
                from datetime import datetime, timedelta, timezone
                now = datetime.now(timezone.utc)
                cutoff_1h = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                cutoff_24h_dt = now - timedelta(hours=24)

                scored = []
                lanes = {}
                for addr in addresses:
                    snap = WalletActivitySnapshot(address=addr)
                    # paper
                    plast = paper_last.get(addr)
                    if plast:
                        snap.paper_trade_within_24h = True
                        if plast >= cutoff_1h:
                            snap.paper_trade_within_1h = True
                    # signals
                    slast = signal_last.get(addr)
                    if slast:
                        snap.signal_within_24h = True
                        if slast >= cutoff_1h:
                            snap.signal_within_1h = True
                    # ras
                    snap.recent_activity_score = ras_map.get(addr, 0.0)
                    lane = snap.classify()
                    lanes[addr] = lane
                    if lane == LANE_HOT:
                        scored.append((addr, snap.compute_score()))

                # Cap HOT top-N by score
                if len(scored) > HOT_LANE_MAX_HOT_COUNT:
                    scored.sort(key=lambda x: -x[1])
                    for addr, _ in scored[HOT_LANE_MAX_HOT_COUNT:]:
                        lanes[addr] = LANE_WARM

                hot_n = sum(1 for v in lanes.values() if v == "HOT")
                warm_n = sum(1 for v in lanes.values() if v == "WARM")
                cold_n = sum(1 for v in lanes.values() if v == "COLD")
                elapsed_ms = int((_time.time() - t0) * 1000)
                logger.info(
                    "hot_lane_classify (batch v2): total=%d HOT=%d WARM=%d COLD=%d cap=%d elapsed=%dms",
                    len(addresses), hot_n, warm_n, cold_n, HOT_LANE_MAX_HOT_COUNT, elapsed_ms,
                )
                # P0.1 instrumentation — dump snapshot for offline analysis.
                # Non-invasive : ne change pas le comportement, juste un fichier.
                try:
                    import json as _json
                    import os as _os
                    _snap_dir = "/opt/app/polyoracle/data/_lane_snapshots"
                    _os.makedirs(_snap_dir, exist_ok=True)
                    _snap = {
                        "snapshot_at": now.isoformat(),
                        "cohort_size": len(addresses),
                        "hot_count": hot_n,
                        "warm_count": warm_n,
                        "cold_count": cold_n,
                        "hot_cap": HOT_LANE_MAX_HOT_COUNT,
                        "elapsed_ms": elapsed_ms,
                        "lanes": {
                            addr: {
                                "lane": lanes.get(addr, "WARM"),
                                "ras": ras_map.get(addr, 0.0),
                                "paper_1h": addr in paper_last and paper_last[addr] >= cutoff_1h,
                                "signal_1h": addr in signal_last and signal_last[addr] >= cutoff_1h,
                            }
                            for addr in addresses
                        },
                    }
                    with open(f"{_snap_dir}/lane_snapshot_latest.json", "w") as fh:
                        _json.dump(_snap, fh)
                    # also keep timestamped copy every 5min for time-series
                    _ts = now.strftime("%Y%m%dT%H%M%SZ")
                    with open(f"{_snap_dir}/lane_snapshot_{_ts}.json", "w") as fh:
                        _json.dump(_snap, fh)
                except Exception as exc:
                    logger.debug("lane_snapshot dump failed (non-fatal): %r", exc)
                return lanes
        except Exception as exc:
            logger.warning("hot_lane classify (batch) failed: %r — fallback WARM", exc)
            return {a: "WARM" for a in addresses}

    def _hot_lane_interval(self, lane: str) -> float:
        if lane == "HOT":
            return HOT_LANE_HOT_INTERVAL_S
        if lane == "COLD":
            return HOT_LANE_COLD_INTERVAL_S
        return HOT_LANE_WARM_INTERVAL_S

    async def run_loop(self) -> None:
        if not self._cohort:
            # v0.7.8 P6 — read capital_total from BotState so load_cohort
            # can apply the tier-specific wallet filter (SMALL → ELITE only).
            # 2026-05-09: effective capital includes session-PnL → cohort auto-ramps.
            with Session(engine) as _cohort_session:
                from app.services.paper_trading_engine import compute_effective_paper_capital
                _capital = compute_effective_paper_capital(_cohort_session)
                self._cohort = self.load_cohort(
                    session=_cohort_session,
                    capital_total=_capital,
                )
                # v0.7.8 — populate the ELITE set so re-polling uses the
                # tighter interval for ELITE wallets.
                self._elite_addresses = self.load_elite_set(_cohort_session)
        if not self._cohort:
            logger.error("polling cohort empty — stopping")
            return
        # 2026-05-18 — Hot lane classification cache. Refreshed every 5min
        # via background task. Falls back to ELITE binary set if disabled.
        self._wallet_lanes: dict[str, str] = {}
        self._lane_last_refresh: float = 0.0
        if SCHEDULER_HOT_LANE_ENABLED:
            self._wallet_lanes = self._classify_cohort_lanes(list(self._cohort))
            self._lane_last_refresh = asyncio.get_event_loop().time()
            hot_count = sum(1 for v in self._wallet_lanes.values() if v == "HOT")
            warm_count = sum(1 for v in self._wallet_lanes.values() if v == "WARM")
            cold_count = sum(1 for v in self._wallet_lanes.values() if v == "COLD")
            logger.info(
                "polling start (HOT LANE ON): pool=%d HOT=%d WARM=%d COLD=%d "
                "intervals HOT=%.0fs WARM=%.0fs COLD=%.0fs rate=%.1f",
                len(self._cohort), hot_count, warm_count, cold_count,
                HOT_LANE_HOT_INTERVAL_S, HOT_LANE_WARM_INTERVAL_S, HOT_LANE_COLD_INTERVAL_S,
                POLLING_RATE_LIMIT_CALLS_PER_SEC,
            )
        else:
            logger.info(
                "polling start: pool=%d, interval_default=%ds, interval_elite=%ds, "
                "rate=%.1f calls/s, elite_count=%d",
                len(self._cohort),
                POLLING_INTERVAL_SECONDS,
                POLLING_INTERVAL_ELITE_SECONDS,
                POLLING_RATE_LIMIT_CALLS_PER_SEC,
                len(self._elite_addresses),
            )
        # P0-B 2026-05-18 — branch on POLLING_WORKERS_ENABLED.
        # Flag ON  → parallel async workers with priority queue (anti-starvation).
        # Flag OFF → legacy single-worker sequential loop (below).
        from app.services import polling_workers as _pw
        if _pw.is_enabled():
            logger.info("polling architecture = WORKERS (P0-B parallel async, flag ON)")
            await self._run_loop_workers()
            return
        logger.info("polling architecture = LEGACY (single-worker, flag OFF)")
        # v0.7.8 — initial stagger uses the per-wallet interval so ELITE
        # wallets are spread across the first ELITE-window (~30s) and
        # non-ELITE wallets are spread across the default window (~90s).
        # That puts every ELITE in the front of the polling queue.
        loop = asyncio.get_event_loop()
        elite_count = sum(1 for a in self._cohort if a in self._elite_addresses)
        non_elite_count = max(1, len(self._cohort) - elite_count)
        next_due: dict[str, float] = {}
        elite_idx = 0
        non_elite_idx = 0
        for addr in self._cohort:
            if addr in self._elite_addresses:
                offset = (POLLING_INTERVAL_ELITE_SECONDS / max(1, elite_count)) * elite_idx
                elite_idx += 1
            else:
                offset = (POLLING_INTERVAL_SECONDS / non_elite_count) * non_elite_idx
                non_elite_idx += 1
            next_due[addr] = loop.time() + offset
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            while not (self._stop_event and self._stop_event.is_set()):
                # In-memory heartbeat for the watchdog — no DB write so a
                # SQLite lock can't suppress it. Watchdog reads this and
                # force-cancels if it goes stale.
                self._iteration_heartbeat = loop.time()
                now = loop.time()
                # Pick the wallet with the smallest next_due.
                addr, due = min(next_due.items(), key=lambda kv: kv[1])
                wait = max(0.0, due - now)
                if wait > 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                        break  # stop signalled during wait
                    except asyncio.TimeoutError:
                        pass
                try:
                    await self.poll_wallet_once(client, addr)
                except Exception as exc:
                    logger.warning(
                        "polling_err site=poll_wallet_outer type=%s wallet=%s err=%r",
                        type(exc).__name__, addr, str(exc),
                    )
                    self._errors_session += 1
                # 2026-05-18 — Hot lane scheduler (if enabled) uses per-wallet
                # lane (HOT/WARM/COLD) instead of binary ELITE/non-ELITE.
                # Refresh lane cache every 5min.
                if SCHEDULER_HOT_LANE_ENABLED:
                    if loop.time() - self._lane_last_refresh > HOT_LANE_REFRESH_INTERVAL_S:
                        self._wallet_lanes = self._classify_cohort_lanes(list(self._cohort))
                        self._lane_last_refresh = loop.time()
                    _lane = self._wallet_lanes.get(addr, "WARM")
                    _interval = self._hot_lane_interval(_lane)
                else:
                    # Legacy v0.7.8 binary ELITE/non-ELITE
                    _interval = (
                        POLLING_INTERVAL_ELITE_SECONDS
                        if addr in self._elite_addresses
                        else POLLING_INTERVAL_SECONDS
                    )
                next_due[addr] = loop.time() + _interval
                # DB writes block the asyncio loop because Session is sync.
                # Throttle to once every 30 polls so we don't serialize the
                # whole cycle on SQLite commits.
                self._poll_count += 1
                if self._poll_count % 30 == 0:
                    await asyncio.to_thread(self._update_db_counters_sync)
                # v0.7.7 B9.1 — periodic background close-loop. Run at
                # most once every BACKGROUND_CLOSE_LOOP_INTERVAL_S seconds
                # so the cap doesn't saturate during long runs.
                await asyncio.to_thread(self._maybe_run_close_loop_sync)
                # v0.7.8 Phase 2 — adaptive close-loop iteration on EVERY
                # polling cycle. Cheap (only runs if a position is due)
                # and replaces the 600s lag for ULTRA_SHORT trades.
                await asyncio.to_thread(self._run_adaptive_close_loop_sync)

    async def _run_loop_workers(self) -> None:
        """P0-B 2026-05-18 — parallel async workers with priority queue.

        Replaces the single-worker sequential loop when POLLING_WORKERS_ENABLED.
        Architecture (see app/services/polling_workers.py docstring):
        - N workers (default 8) consume a LanePriorityQueue.
        - Workers do ONLY: get → rate-limit → poll → process → update_state → requeue.
        - Close-loops + DB counter updater run as SEPARATE background coroutines,
          NOT inside workers (avoids re-introducing the blocking that the legacy
          loop suffered from).
        - Shared TokenBucket (self.bucket) enforces global 18 calls/s.
        - Priority HOT > WARM > COLD with anti-starvation (2× interval escalation).
        """
        from app.services import polling_workers as _pw
        loop = asyncio.get_event_loop()

        # Build wallet→lane map (uses hot lane classifier if enabled, else binary)
        if SCHEDULER_HOT_LANE_ENABLED:
            self._wallet_lanes = self._classify_cohort_lanes(list(self._cohort))
            self._lane_last_refresh = loop.time()
        else:
            # Map ELITE → HOT, non-ELITE → WARM for backwards-compat shape
            self._wallet_lanes = {
                a: ("HOT" if a in self._elite_addresses else "WARM")
                for a in self._cohort
            }
            self._lane_last_refresh = loop.time()

        # Initial intervals for the queue
        hot_int = HOT_LANE_HOT_INTERVAL_S if SCHEDULER_HOT_LANE_ENABLED else POLLING_INTERVAL_ELITE_SECONDS
        warm_int = HOT_LANE_WARM_INTERVAL_S if SCHEDULER_HOT_LANE_ENABLED else POLLING_INTERVAL_SECONDS
        cold_int = HOT_LANE_COLD_INTERVAL_S if SCHEDULER_HOT_LANE_ENABLED else POLLING_INTERVAL_SECONDS * 2

        queue = _pw.LanePriorityQueue()
        queue.set_intervals(hot=hot_int, warm=warm_int, cold=cold_int)

        # Stagger initial dues: spread by lane within each interval so first
        # pass isn't a thundering herd on Polymarket API.
        n_workers = _pw.workers_count()
        hot_addrs = [a for a, l in self._wallet_lanes.items() if l == "HOT"]
        warm_addrs = [a for a, l in self._wallet_lanes.items() if l == "WARM"]
        cold_addrs = [a for a, l in self._wallet_lanes.items() if l == "COLD"]

        def _stagger(addrs, interval):
            n = max(1, len(addrs))
            return [(addr, idx * interval / n) for idx, addr in enumerate(addrs)]

        for addr, offset in _stagger(hot_addrs, hot_int):
            await queue.put(addr, "HOT", loop.time() + offset)
        for addr, offset in _stagger(warm_addrs, warm_int):
            await queue.put(addr, "WARM", loop.time() + offset)
        for addr, offset in _stagger(cold_addrs, cold_int):
            await queue.put(addr, "COLD", loop.time() + offset)

        logger.info(
            "P0-B workers: pool=%d cohort=%d HOT=%d WARM=%d COLD=%d "
            "intervals HOT=%.0fs WARM=%.0fs COLD=%.0fs rate=%.1f",
            n_workers, len(self._cohort), len(hot_addrs), len(warm_addrs),
            len(cold_addrs), hot_int, warm_int, cold_int,
            POLLING_RATE_LIMIT_CALLS_PER_SEC,
        )

        # Single shared httpx client passed to poll_wallet_once via closure.
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:

            async def poll_func(addr: str) -> None:
                await self.poll_wallet_once(client, addr)
                self._poll_count += 1

            def interval_func(lane: str) -> float:
                return self._hot_lane_interval(lane) if SCHEDULER_HOT_LANE_ENABLED else (
                    POLLING_INTERVAL_ELITE_SECONDS if lane == "HOT" else POLLING_INTERVAL_SECONDS
                )

            pool = _pw.WorkerPool(
                queue=queue,
                rate_limiter=self.bucket,
                poll_func=poll_func,
                interval_func=interval_func,
                n_workers=n_workers,
            )
            # Expose pool for observability endpoint
            self._worker_pool = pool

            # Background tasks (NOT in workers): close-loops + DB counter +
            # lane refresh + heartbeat tick for watchdog.
            stop_bg = asyncio.Event()

            async def bg_close_loop() -> None:
                while not stop_bg.is_set():
                    try:
                        await asyncio.to_thread(self._maybe_run_close_loop_sync)
                        await asyncio.to_thread(self._run_adaptive_close_loop_sync)
                    except Exception as exc:
                        logger.warning("bg_close_loop err: %r", exc)
                    try:
                        await asyncio.wait_for(stop_bg.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass

            async def bg_db_counter() -> None:
                while not stop_bg.is_set():
                    try:
                        await asyncio.to_thread(self._update_db_counters_sync)
                    except Exception as exc:
                        logger.warning("bg_db_counter err: %r", exc)
                    try:
                        await asyncio.wait_for(stop_bg.wait(), timeout=30.0)
                    except asyncio.TimeoutError:
                        pass

            async def bg_heartbeat() -> None:
                while not stop_bg.is_set():
                    self._iteration_heartbeat = asyncio.get_event_loop().time()
                    try:
                        await asyncio.wait_for(stop_bg.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass

            bg_tasks = [
                asyncio.create_task(bg_close_loop(), name="bg_close_loop"),
                asyncio.create_task(bg_db_counter(), name="bg_db_counter"),
                asyncio.create_task(bg_heartbeat(), name="bg_heartbeat"),
            ]
            await pool.start()

            try:
                # Wait until stop_event is signalled.
                if self._stop_event is not None:
                    await self._stop_event.wait()
            finally:
                logger.info("P0-B workers: stopping...")
                stop_bg.set()
                await pool.stop()
                for t in bg_tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*bg_tasks, return_exceptions=True)
                logger.info("P0-B workers: stopped cleanly. Final stats: %s", pool.stats)

    def _run_adaptive_close_loop_sync(self) -> None:
        """v0.7.8 Phase 2 — adaptive close-loop iteration. Pop due
        positions, check market state, close resolved. Cheap if no
        positions are due (heap O(log N) check).
        """
        try:
            from app.services.adaptive_close_scheduler import (
                run_adaptive_close_iteration,
            )
            with Session(engine) as _session:
                # v0.7.8 P6 + 2026-05-06 polling 10s upgrade:
                # Worst case must stay < polling interval (10s ELITE) to
                # avoid B19 watchdog stall. max_entries=2 × 2 HTTP × 2s
                # gamma_timeout = ~8s, fits in 10s budget with margin.
                # Scaled down from (3, 3.0)=18s which broke the 10s polling.
                result = run_adaptive_close_iteration(
                    _session, max_entries=2, gamma_timeout=2.0,
                )
                if result.get("closed", 0) > 0:
                    self._close_loop_total_closed += int(result["closed"])
                    logger.info(
                        "adaptive_close: checked=%d closed=%d rescheduled=%d",
                        result["checked"], result["closed"], result["rescheduled"],
                    )
        except Exception as exc:
            # Note: this catch swallows Gamma HTTP errors / circuit-breaker
            # RuntimeError from the per-position loop. It does NOT bump
            # _errors_session, so the orchestrator polling_errors count
            # won't reflect close-loop HTTP slowdowns. Stall hypothesis:
            # if Gamma is slow, this whole sync helper blocks the polling
            # iteration via asyncio.to_thread for up to ~10s/position.
            logger.warning(
                "polling_err site=adaptive_close_iter type=%s err=%r",
                type(exc).__name__, str(exc),
            )

    def _maybe_run_close_loop_sync(self) -> None:
        """v0.7.7 B9.1 — periodic close-loop. Idempotent if not yet due.

        Fires ``run_background_close_loop_iteration`` at most once per
        ``BACKGROUND_CLOSE_LOOP_INTERVAL_S`` seconds. Tracks total closed
        in ``self._close_loop_total_closed`` for /status reporting.
        """
        from app.services.paper_trading_engine import (
            run_background_close_loop_iteration,
            BACKGROUND_CLOSE_LOOP_INTERVAL_S,
        )
        now_unix = int(datetime.now(UTC).timestamp())
        if (now_unix - self._last_close_loop_unix) < BACKGROUND_CLOSE_LOOP_INTERVAL_S:
            return
        try:
            with Session(engine) as session:
                result = run_background_close_loop_iteration(session)
                self._close_loop_total_closed += int(result.get("total_closed") or 0)
                if result.get("total_closed"):
                    logger.info(
                        "B9.1 close-loop fired: closed=%s (resolved=%s, stale_ttl=%s)",
                        result.get("total_closed"),
                        result.get("closed_resolved"),
                        result.get("closed_stale_ttl"),
                    )
        except Exception as exc:
            logger.warning("B9.1 close-loop error: %s", exc)
        self._last_close_loop_unix = now_unix

    def _update_db_counters_sync(self) -> None:
        """Sync version called from a worker thread so SQLite commits don't
        block the polling event loop. Persists session counters to the DB
        snapshot so they survive a backend restart.
        """
        with Session(engine) as session:
            state = session.get(BotLoopState, 1)
            if state is None:
                state = BotLoopState(id=1)
            state.polling_running = True
            state.last_poll_at = datetime.now(UTC)
            state.wallets_polled_count = len(self._cohort)
            state.trades_detected_total = (state.trades_detected_total or 0) + self._trades_detected_session
            state.paper_trades_via_polling = (state.paper_trades_via_polling or 0) + self._paper_trades_session
            state.polling_errors_count = (state.polling_errors_count or 0) + self._errors_session
            state.updated_at = datetime.now(UTC)
            session.add(state)
            session.commit()
        # Reset session counters now that they're persisted.
        self._trades_detected_session = 0
        self._paper_trades_session = 0
        self._errors_session = 0

    async def _update_db_counters(self) -> None:
        await asyncio.to_thread(self._update_db_counters_sync)

    # ---------------- start/stop ----------------

    async def start(self) -> dict[str, Any]:
        if self._task and not self._task.done():
            return self.status()
        self._stop_event = asyncio.Event()
        # v0.7.8 P6 — capital-tier aware cohort + priority ORDER BY.
        # 2026-05-09: effective capital includes session-PnL → cohort auto-ramps.
        with Session(engine) as _cohort_session:
            from app.services.paper_trading_engine import compute_effective_paper_capital
            _capital = compute_effective_paper_capital(_cohort_session)
            self._cohort = self.load_cohort(
                session=_cohort_session,
                capital_total=_capital,
            )
        # Pin the start time so every wallet's first-contact uses the same
        # baseline regardless of when in the staggered cycle it lands.
        self._polling_start_unix = int(datetime.now(UTC).timestamp())
        with Session(engine) as session:
            state = session.get(BotLoopState, 1)
            if state is None:
                state = BotLoopState(id=1)
            state.polling_running = True
            state.polling_started_at = datetime.now(UTC)
            state.wallets_polled_count = len(self._cohort)
            session.add(state)
            session.commit()
            AuditLogger(session).log("POLLING_START", "wallet_polling_engine", result=str(len(self._cohort)))
        # v0.7.8 P6 B18: re-register open positions from DB into the
        # adaptive close scheduler so they get checked after restart.
        with Session(engine) as _boot_session:
            from app.services.adaptive_close_scheduler import boot_register_open_positions
            boot_register_open_positions(_boot_session)
        self._task = asyncio.create_task(self.run_loop())
        self._task.add_done_callback(self._on_task_done)
        self._iteration_heartbeat = asyncio.get_event_loop().time()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        return self.status()

    async def _watchdog_loop(self) -> None:
        """Detect silent stalls (deadlock/long-blocking-sync) where run_loop
        keeps awaiting but never advances. Reads the in-memory heartbeat
        every WATCHDOG_TICK_S; force-cancels the polling task if stale
        beyond WATCHDOG_STALL_S. The done_callback then surfaces the
        CancelledError so the orchestrator can react."""
        WATCHDOG_TICK_S = 30.0
        WATCHDOG_STALL_S = 120.0
        try:
            while self._task is not None and not self._task.done():
                await asyncio.sleep(WATCHDOG_TICK_S)
                if self._task is None or self._task.done():
                    return
                loop = asyncio.get_event_loop()
                age = loop.time() - self._iteration_heartbeat
                if age > WATCHDOG_STALL_S:
                    logger.error(
                        "watchdog: polling stalled for %.1fs (>%.0fs threshold) — cancelling task",
                        age, WATCHDOG_STALL_S,
                    )
                    try:
                        with Session(engine) as _session:
                            state = _session.get(BotLoopState, 1)
                            if state is not None:
                                state.last_polling_error = (
                                    f"WATCHDOG_STALL stalled_for={age:.1f}s"
                                )
                                _session.add(state)
                                _session.commit()
                    except Exception as db_exc:
                        logger.error("watchdog could not persist stall: %s", db_exc)
                    self._task.cancel()
                    return
        except asyncio.CancelledError:
            return

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        # Silent-stall diagnostic: surface any exception that killed run_loop
        # so the orchestrator/log shows it instead of swallowing it. The DB
        # write is wrapped because if the original stall is itself a SQLite
        # lock, this callback would hang/fail silently — losing the very
        # signal we added it for. Logging is the must-have, DB is best-effort.
        if task.cancelled():
            logger.warning("polling task cancelled")
            return
        exc = task.exception()
        if exc is not None:
            logger.error("polling task died with unhandled exception", exc_info=exc)
            try:
                with Session(engine) as _session:
                    state = _session.get(BotLoopState, 1)
                    if state is not None:
                        state.last_polling_error = f"{type(exc).__name__}: {exc}"[:500]
                        state.polling_running = False
                        _session.add(state)
                        _session.commit()
            except Exception as db_exc:
                logger.error("polling _on_task_done could not persist error: %s", db_exc)

    async def stop(self) -> dict[str, Any]:
        if self._stop_event:
            self._stop_event.set()
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
        with Session(engine) as session:
            state = session.get(BotLoopState, 1)
            if state is None:
                state = BotLoopState(id=1)
            state.polling_running = False
            session.add(state)
            session.commit()
            AuditLogger(session).log("POLLING_STOP", "wallet_polling_engine", result="stopped")
        return self.status()

    def status(self) -> dict[str, Any]:
        with Session(engine) as session:
            state = session.get(BotLoopState, 1)
            if state is None:
                state = BotLoopState(id=1)
            return {
                "running": bool(state.polling_running),
                "started_at": state.polling_started_at.isoformat() if state.polling_started_at else None,
                "last_poll_at": state.last_poll_at.isoformat() if state.last_poll_at else None,
                "wallets_polled_count": state.wallets_polled_count or 0,
                "trades_detected_total": (state.trades_detected_total or 0) + self._trades_detected_session,
                "paper_trades_opened": (state.paper_trades_via_polling or 0) + self._paper_trades_session,
                "polling_errors": (state.polling_errors_count or 0) + self._errors_session,
                "session_trades_detected": self._trades_detected_session,
                "session_paper_trades": self._paper_trades_session,
                "session_errors": self._errors_session,
                "cohort_size": len(self._cohort),
                "interval_seconds": POLLING_INTERVAL_SECONDS,
                "interval_seconds_elite": POLLING_INTERVAL_ELITE_SECONDS,
                "rate_limit_calls_per_sec": POLLING_RATE_LIMIT_CALLS_PER_SEC,
            }

    @classmethod
    async def maybe_resume(cls) -> None:
        """Called from FastAPI startup. Restarts the polling loop if a
        prior process flagged it running.
        """
        with Session(engine) as session:
            state = session.get(BotLoopState, 1)
            should_resume = bool(state and state.polling_running)
        if not should_resume:
            return
        try:
            await cls.instance().start()
            logger.info("polling auto-resumed")
        except Exception as exc:
            logger.warning("polling auto-resume failed: %s", exc)


def reset_singleton_for_tests() -> None:
    """Test helper — drop the cached singleton."""
    with WalletPollingEngine._instance_lock:
        WalletPollingEngine._instance = None
