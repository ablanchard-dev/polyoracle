"""P0-B baseline polling delay observer (2026-05-18).

Log-only instrumentation that captures one JSONL record per pipeline pass in
``_process_trade_in_session``. Records timestamps (source trade → first seen →
audit → open/reject), outcome, reject reason, orderbook quality, copyable edge,
spread, liquidity, notional, lane (best-effort), wallet bucket.

Strict design rules:
- **No scheduling, cadence, gate, or sizing change.** Pure observability.
- Feature flag ``POLLING_DELAY_OBS_ENABLED`` (default ``false``) — read on every
  call so flip + bot restart toggles in <30s without code change.
- Every public function is ``try/except``-wrapped; failures emit ``logger.debug``
  and return ``None``. Never raise into the calling pipeline.
- JSONL append, one file per hour: ``data/_polling_delay_obs/delay_obs_YYYYMMDDTHHZ.jsonl``.

Analysis (post-collect) is done outside the bot — operator pulls JSONL into
pandas / DuckDB and computes p50/p90/p99 source→seen per lane, captures/h
per lane, reject reason distribution per lane, etc.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OBS_DIR = Path(__file__).resolve().parents[3] / "data" / "_polling_delay_obs"

# ContextVar lets in-pipeline code annotate the observation dict without
# threading it through every function signature.
_active_obs: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "polling_delay_obs", default=None
)

# Set of common reject reason codes for lane lookup (no effect on bot logic).
_KNOWN_REASON_CODES: set[str] = set()


def is_enabled() -> bool:
    """Read feature flag on every call (cheap). Default ``false``."""
    try:
        return os.environ.get("POLLING_DELAY_OBS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _now_ms() -> int:
    return int(time.time() * 1000)


def start_observation(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Begin a new observation. Returns the obs dict (or None if disabled/error).
    Also sets the contextvar so in-pipeline hooks (attach_audit_active, etc.)
    can decorate it without needing the dict passed explicitly."""
    if not is_enabled() or raw is None:
        _active_obs.set(None)
        return None
    try:
        src_ts = raw.get("timestamp")
        try:
            src_ts_int = int(src_ts) if src_ts is not None else 0
        except (TypeError, ValueError):
            src_ts_int = 0
        obs: dict[str, Any] = {
            "source_trade_ts_unix": src_ts_int,
            "first_seen_at_ms": _now_ms(),
            "wallet_address": raw.get("wallet_address") or raw.get("proxyWallet") or None,
            "market_id": raw.get("market_id") or raw.get("conditionId") or None,
            "outcome_side": raw.get("side"),
            "outcome": raw.get("outcome"),
            "raw_price": raw.get("price"),
        }
        _active_obs.set(obs)
        return obs
    except Exception as exc:
        logger.debug("polling_delay_obs start failed: %r", exc)
        _active_obs.set(None)
        return None


def attach_audit_active(audit_record: Any) -> None:
    """Thread-safe in-pipeline hook. Annotates the active observation (if any)
    with audit fields. Safe to call unconditionally — no-op if obs not active."""
    try:
        obs = _active_obs.get()
        if obs is None:
            return
        attach_audit(obs, audit_record)
    except Exception as exc:
        logger.debug("polling_delay_obs attach_audit_active failed: %r", exc)


def attach_wallet_active(wallet_record: Any) -> None:
    """In-pipeline hook for wallet classification."""
    try:
        obs = _active_obs.get()
        if obs is None:
            return
        attach_wallet(obs, wallet_record)
    except Exception as exc:
        logger.debug("polling_delay_obs attach_wallet_active failed: %r", exc)


def attach_audit(obs: dict[str, Any] | None, audit_record: Any) -> None:
    """Annotate obs with audit fields (orderbook, edge, spread, etc.). Idempotent."""
    if obs is None or audit_record is None:
        return
    try:
        obs["audit_attached_at_ms"] = _now_ms()
        obs["audit_decision"] = getattr(audit_record, "decision", None)
        obs["copy_delay_seconds_audit"] = float(getattr(audit_record, "copy_delay_seconds", 0) or 0)
        obs["copyable_edge"] = float(getattr(audit_record, "copyable_edge", 0) or 0)
        obs["orderbook_quality"] = getattr(audit_record, "orderbook_quality", None)
        obs["trade_quality_score"] = float(getattr(audit_record, "trade_quality_score", 0) or 0)
        obs["wallet_tier"] = getattr(audit_record, "wallet_tier", None)
        obs["notional_usd"] = float(getattr(audit_record, "notional_usd", 0) or 0)
        obs["spread"] = float(getattr(audit_record, "spread", 0) or 0)
        obs["liquidity_score"] = float(getattr(audit_record, "market_liquidity_score", 0) or 0)
    except Exception as exc:
        logger.debug("polling_delay_obs attach_audit failed: %r", exc)


def attach_wallet(obs: dict[str, Any] | None, wallet_record: Any) -> None:
    """Annotate obs with wallet classification (candidate_status, bucket)."""
    if obs is None or wallet_record is None:
        return
    try:
        obs["candidate_status"] = getattr(wallet_record, "candidate_status", None)
        wr = getattr(wallet_record, "resolved_market_win_rate", None)
        if wr is None:
            bucket = "UNKNOWN"
        else:
            wr_f = float(wr)
            if wr_f >= 0.99:
                bucket = "GOLD"
            elif wr_f >= 0.95:
                bucket = "SILVER"
            elif wr_f >= 0.90:
                bucket = "BRONZE"
            else:
                bucket = "REG"
        obs["wallet_bucket"] = bucket
        obs["wallet_winrate"] = float(wr) if wr is not None else None
    except Exception as exc:
        logger.debug("polling_delay_obs attach_wallet failed: %r", exc)


def attach_lane(obs: dict[str, Any] | None, lane: str | None) -> None:
    """Annotate obs with the wallet's polling lane (HOT/WARM/COLD/legacy)."""
    if obs is None:
        return
    try:
        obs["lane"] = lane if lane else "unknown"
    except Exception as exc:
        logger.debug("polling_delay_obs attach_lane failed: %r", exc)


def finalize_from_result(obs: dict[str, Any] | None, result: dict[str, Any] | None) -> None:
    """Write the JSONL record. Called from finally block after pipeline returns."""
    if obs is None:
        return
    try:
        decision_at_ms = _now_ms()
        obs["decision_at_ms"] = decision_at_ms

        # Derive outcome from result dict
        if result is None:
            outcome = "ERROR_NO_RESULT"
            reason_code = None
            paper_opened_at_ms: int | None = None
        elif result.get("executed") is True:
            outcome = "OPENED"
            reason_code = None
            paper_opened_at_ms = decision_at_ms  # approximation
        elif result.get("rejected") is True or result.get("executed") is False:
            outcome = "REJECTED"
            reason_code = result.get("reason_code") or result.get("reason")
            paper_opened_at_ms = None
        else:
            outcome = "UNKNOWN"
            reason_code = None
            paper_opened_at_ms = None

        obs["decision_outcome"] = outcome
        obs["reject_reason_code"] = reason_code
        obs["paper_opened_at_ms"] = paper_opened_at_ms

        # Compute delays (all in ms)
        source_ms = obs["source_trade_ts_unix"] * 1000 if obs.get("source_trade_ts_unix") else 0
        first_seen_ms = obs.get("first_seen_at_ms")
        audit_ms = obs.get("audit_attached_at_ms")

        if source_ms and first_seen_ms:
            obs["delay_source_to_seen_ms"] = first_seen_ms - source_ms
        if first_seen_ms and audit_ms:
            obs["delay_seen_to_audit_ms"] = audit_ms - first_seen_ms
        if audit_ms and paper_opened_at_ms:
            obs["delay_audit_to_open_ms"] = paper_opened_at_ms - audit_ms
        if source_ms:
            obs["delay_total_ms"] = decision_at_ms - source_ms

        # Write JSONL with hourly rotation
        _OBS_DIR.mkdir(parents=True, exist_ok=True)
        hour = datetime.now(UTC).strftime("%Y%m%dT%HZ")
        path = _OBS_DIR / f"delay_obs_{hour}.jsonl"
        line = json.dumps(obs, default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        logger.debug("polling_delay_obs finalize failed: %r", exc)
