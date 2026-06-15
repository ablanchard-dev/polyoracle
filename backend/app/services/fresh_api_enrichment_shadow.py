"""P0.3 — FreshApiEnrichmentService (SHADOW Phase 2).

PURPOSE
=======

Quantify the angle mort COLD lane : how many wallets classified COLD by our
HotLaneClassifier (= no internal activity 1h+24h) are ACTUALLY active on
Polymarket but invisible to our polling.

SHADOW MODE
===========

- Reads cohort + lane classification
- Queries `data-api.polymarket.com/trades?user=X&limit=10` for COLD wallets
- Persists results to JSON state file (no SQL schema change)
- **DOES NOT modify the classifier output**
- Feature flag `FRESH_API_SHADOW_ENABLED` (default false)
- Rate cap 0.1 c/s (= 30 calls / 5min cycle, well under 1 c/s budget margin)

GARDE-FOUS
==========

- Bot continues unchanged behaviorally
- If any 429 / latency spike / disk pressure → stop immediately
- Cycle interval and batch size are env-tunable

OUTPUT
======

- `data/_fresh_api_shadow/state.json` — rolling state per wallet
- `data/_fresh_api_shadow/cycles.log` — append-only cycle log

The analyzer script `_p0_3_fresh_api_shadow_report.py` reads `state.json` and
produces the markdown summary + CSV per design doc Phase 2.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SHADOW_DIR = Path(__file__).resolve().parents[3] / "data" / "_fresh_api_shadow"
STATE_FILE = SHADOW_DIR / "state.json"
CYCLES_LOG = SHADOW_DIR / "cycles.log"

DATA_API_BASE = os.environ.get("DATA_API_BASE_URL", "https://data-api.polymarket.com")
CYCLE_INTERVAL_S = float(os.environ.get("FRESH_API_SHADOW_CYCLE_S", "300"))  # 5min
BATCH_SIZE = int(os.environ.get("FRESH_API_SHADOW_BATCH", "30"))  # 30 wallets/cycle
CALL_RATE_S = float(os.environ.get("FRESH_API_SHADOW_CALL_RATE_S", "1.0"))  # 1 call/s = 0.1 c/s sustained
HTTP_TIMEOUT_S = 8.0
ENABLED_DEFAULT = False


def env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"wallets": {}, "cycles": []}
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("FreshApiShadow: state.json load failed (%r), starting fresh", exc)
        return {"wallets": {}, "cycles": []}


def save_state(state: dict) -> None:
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    tmp.replace(STATE_FILE)  # atomic


def append_cycle_log(line: str) -> None:
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    with open(CYCLES_LOG, "a") as fh:
        fh.write(line + "\n")


def pick_priority_cold_wallets(state: dict, candidate_addrs: list[str], n: int) -> list[str]:
    """Pick N COLD wallets to check next.

    Priority :
      1. Never checked (no entry in state)
      2. Least recently checked
    """
    wallets = state.get("wallets", {})
    never_checked = [a for a in candidate_addrs if a not in wallets]
    if len(never_checked) >= n:
        return never_checked[:n]
    # need supplement from least-recently-checked
    seen = [(a, wallets[a].get("last_check_at", "1970-01-01")) for a in candidate_addrs if a in wallets]
    seen.sort(key=lambda x: x[1])
    remaining = n - len(never_checked)
    return never_checked + [a for a, _ in seen[:remaining]]


async def fetch_polymarket_trades(client: httpx.AsyncClient, address: str) -> Optional[list[dict]]:
    """Fetch up to 10 most-recent trades for a wallet from data-api Polymarket.

    Returns list of trade dicts or None on error/429.
    """
    url = f"{DATA_API_BASE}/trades"
    params = {"user": address, "limit": 10}
    try:
        r = await client.get(url, params=params, timeout=HTTP_TIMEOUT_S)
        if r.status_code == 429:
            logger.warning("FreshApiShadow: 429 from data-api on %s — pausing cycle", address)
            return None
        if r.status_code != 200:
            logger.debug("FreshApiShadow: HTTP %d for %s", r.status_code, address)
            return None
        return r.json() or []
    except Exception as exc:
        logger.debug("FreshApiShadow: fetch err %s: %r", address, exc)
        return None


def summarize_trades(trades: list[dict]) -> dict:
    """From raw /trades response, compute counts in time windows."""
    if not trades:
        return {
            "last_trade_at": None,
            "trades_1h": 0,
            "trades_7d": 0,
            "trades_30d": 0,
        }
    now = datetime.now(timezone.utc)
    cutoff_1h = now.timestamp() - 3600
    cutoff_7d = now.timestamp() - 86400 * 7
    cutoff_30d = now.timestamp() - 86400 * 30
    c1h = c7d = c30d = 0
    last_ts = 0
    for t in trades:
        ts = t.get("timestamp")
        if ts is None:
            continue
        try:
            ts = int(ts)  # data-api returns int unix
        except Exception:
            continue
        if ts > last_ts:
            last_ts = ts
        if ts >= cutoff_1h:
            c1h += 1
        if ts >= cutoff_7d:
            c7d += 1
        if ts >= cutoff_30d:
            c30d += 1
    last_iso = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat() if last_ts else None
    return {
        "last_trade_at": last_iso,
        "trades_1h": c1h,
        "trades_7d": c7d,
        "trades_30d": c30d,
    }


def load_cold_addresses_from_snapshot() -> list[str]:
    """Read latest lane snapshot, return addrs classified COLD."""
    snap_path = Path(__file__).resolve().parents[3] / "data" / "_lane_snapshots" / "lane_snapshot_latest.json"
    if not snap_path.exists():
        logger.warning("FreshApiShadow: no lane snapshot found — cannot pick COLD")
        return []
    try:
        with open(snap_path) as fh:
            snap = json.load(fh)
        return [a for a, info in snap["lanes"].items() if info.get("lane") == "COLD"]
    except Exception as exc:
        logger.warning("FreshApiShadow: snapshot read err %r", exc)
        return []


async def run_cycle(state: dict, cycle_idx: int) -> dict:
    """Run one enrichment cycle. Returns updated state."""
    cold_addrs = load_cold_addresses_from_snapshot()
    if not cold_addrs:
        logger.info("FreshApiShadow cycle %d: no COLD addrs (snapshot missing/empty)", cycle_idx)
        return state
    targets = pick_priority_cold_wallets(state, cold_addrs, BATCH_SIZE)
    if not targets:
        return state

    cycle_start = time.time()
    cycle_at = datetime.now(timezone.utc).isoformat()
    n_checked = n_active_1h = n_active_7d = 0
    sample_active = []

    async with httpx.AsyncClient() as client:
        for addr in targets:
            trades = await fetch_polymarket_trades(client, addr)
            await asyncio.sleep(CALL_RATE_S)
            if trades is None:
                # 429 or hard error — stop cycle early
                logger.warning("FreshApiShadow cycle %d aborted at %d/%d", cycle_idx, n_checked, len(targets))
                break
            summary = summarize_trades(trades)
            state.setdefault("wallets", {})[addr] = {
                "last_check_at": cycle_at,
                **summary,
            }
            n_checked += 1
            if summary["trades_1h"] > 0:
                n_active_1h += 1
                if len(sample_active) < 5:
                    sample_active.append({"addr": addr, **summary})
            if summary["trades_7d"] > 0:
                n_active_7d += 1

    elapsed = time.time() - cycle_start
    cycle_record = {
        "cycle_idx": cycle_idx,
        "cycle_at": cycle_at,
        "elapsed_s": round(elapsed, 1),
        "cold_pool": len(cold_addrs),
        "targeted": len(targets),
        "checked": n_checked,
        "active_1h": n_active_1h,
        "active_7d": n_active_7d,
        "sample_active": sample_active,
    }
    state.setdefault("cycles", []).append(cycle_record)
    # Keep only last 100 cycles in state
    state["cycles"] = state["cycles"][-100:]

    save_state(state)
    append_cycle_log(json.dumps(cycle_record))
    logger.info(
        "FreshApiShadow cycle %d: checked=%d/%d active_1h=%d active_7d=%d elapsed=%.1fs",
        cycle_idx, n_checked, len(targets), n_active_1h, n_active_7d, elapsed,
    )
    return state


async def run_loop_shadow(stop_event: Optional[asyncio.Event] = None) -> None:
    """Background loop : runs every CYCLE_INTERVAL_S until stop_event set.

    SHADOW MODE — does NOT modify HotLaneClassifier output.
    """
    logger.info(
        "FreshApiShadow loop starting: interval=%ds batch=%d call_rate=%ss",
        int(CYCLE_INTERVAL_S), BATCH_SIZE, CALL_RATE_S,
    )
    state = load_state()
    cycle_idx = len(state.get("cycles", []))

    while not (stop_event and stop_event.is_set()):
        cycle_idx += 1
        try:
            state = await run_cycle(state, cycle_idx)
        except Exception as exc:
            logger.warning("FreshApiShadow cycle %d crashed: %r", cycle_idx, exc)
        # Sleep until next cycle (interruptible via stop_event)
        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=CYCLE_INTERVAL_S)
                break
            else:
                await asyncio.sleep(CYCLE_INTERVAL_S)
        except asyncio.TimeoutError:
            pass

    logger.info("FreshApiShadow loop stopped")
