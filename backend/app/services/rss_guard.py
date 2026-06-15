"""D5 RSS guard (Phase D 2026-05-11).

Background async task that monitors backend RSS memory usage. If RSS
exceeds RSS_GUARD_LIMIT_MB sustained over RSS_GUARD_SUSTAINED_S seconds,
emits a critical log + (optionally) triggers a graceful restart via
SystemExit so systemd D1 brings the backend back up.

Designed to defuse memory leaks at scale (route export_trade_journal,
reclass bulk, market_first utility — see B20 history). Passive at small
volume, only acts when threshold breached for sustained period.

Activation (in app/main.py @ startup):

    from app.services.rss_guard import start_rss_guard_task

    @app.on_event("startup")
    async def startup_rss_guard():
        start_rss_guard_task()

Tunables via env:
  RSS_GUARD_LIMIT_MB         (default 2048 — 2 GB)
  RSS_GUARD_SUSTAINED_S      (default 120 — 2 min)
  RSS_GUARD_CHECK_INTERVAL_S (default 30)
  RSS_GUARD_TRIGGER_EXIT     (default 1 — set to 0 for log-only mode)
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Read at module import — operator can override via env at process start
RSS_GUARD_LIMIT_MB = int(os.environ.get("RSS_GUARD_LIMIT_MB", "2048"))
RSS_GUARD_SUSTAINED_S = int(os.environ.get("RSS_GUARD_SUSTAINED_S", "120"))
RSS_GUARD_CHECK_INTERVAL_S = int(os.environ.get("RSS_GUARD_CHECK_INTERVAL_S", "30"))
RSS_GUARD_TRIGGER_EXIT = bool(int(os.environ.get("RSS_GUARD_TRIGGER_EXIT", "1")))

_RSS_LOG = Path(__file__).resolve().parents[2] / "_smoke_strict_logs" / "rss_guard.log"


def _read_rss_kb() -> int:
    """Read current process RSS via /proc (Linux). Returns 0 if unavailable."""
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])  # kB
    except Exception:
        pass
    return 0


def _log(msg: str) -> None:
    line = f"[{datetime.now(UTC).isoformat(timespec='seconds')}] [rss_guard] {msg}\n"
    try:
        _RSS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_RSS_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass
    sys.stderr.write(line)


async def _rss_guard_loop() -> None:
    breach_since: float | None = None
    last_log_ts: float = 0.0
    _log(
        f"RSS guard started: limit={RSS_GUARD_LIMIT_MB}MB sustained={RSS_GUARD_SUSTAINED_S}s "
        f"interval={RSS_GUARD_CHECK_INTERVAL_S}s trigger_exit={RSS_GUARD_TRIGGER_EXIT}"
    )
    while True:
        rss_kb = _read_rss_kb()
        rss_mb = rss_kb / 1024
        now = asyncio.get_event_loop().time()

        # Throttle informational logs (every 5 min)
        if now - last_log_ts > 300:
            _log(f"rss_mb={rss_mb:.0f}")
            last_log_ts = now

        if rss_mb > RSS_GUARD_LIMIT_MB:
            if breach_since is None:
                breach_since = now
                _log(f"BREACH START: rss={rss_mb:.0f}MB > {RSS_GUARD_LIMIT_MB}MB")
            elif now - breach_since >= RSS_GUARD_SUSTAINED_S:
                _log(
                    f"BREACH SUSTAINED ({now - breach_since:.0f}s ≥ {RSS_GUARD_SUSTAINED_S}s) — "
                    f"rss={rss_mb:.0f}MB"
                )
                if RSS_GUARD_TRIGGER_EXIT:
                    _log("triggering graceful exit — systemd D1 will restart backend")
                    # SystemExit raised in main loop → uvicorn shuts down → systemd restarts
                    os._exit(42)
                else:
                    _log("trigger_exit disabled — log-only mode")
        else:
            if breach_since is not None:
                _log(f"BREACH RECOVERED: rss={rss_mb:.0f}MB back under limit")
                breach_since = None

        await asyncio.sleep(RSS_GUARD_CHECK_INTERVAL_S)


def start_rss_guard_task() -> asyncio.Task[None]:
    """Schedule the RSS guard as a background task. Caller (main.py @startup)
    keeps a reference to prevent garbage collection."""
    return asyncio.create_task(_rss_guard_loop(), name="rss_guard")
