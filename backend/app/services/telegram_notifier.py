"""D6 Telegram notifier (Phase D 2026-05-11).

Stub-ready service for Telegram alerts. Default = log-only mode (TELEGRAM_TOKEN
not configured → never sends). Once operator adds bot token + chat_id,
becomes active without code changes.

Alert categories (per plan D6):
  - crash backend
  - DD breach (>5% sub HWM)
  - kill_switch activé
  - polling stall (>2min)
  - vault transfer success/fail
  - daily report résumé

Throttling: per-event-type cooldown (default 5 min) to avoid spam.

Env config:
  TELEGRAM_BOT_TOKEN  (required for sending)
  TELEGRAM_CHAT_ID    (required for sending)
  TELEGRAM_THROTTLE_S (default 300 = 5 min per event type)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

EventType = Literal[
    "BACKEND_CRASH",
    "DD_BREACH",
    "KILL_SWITCH",
    "POLLING_STALL",
    "VAULT_TRANSFER_OK",
    "VAULT_TRANSFER_FAIL",
    "DAILY_REPORT",
    "RSS_BREACH",
    "STRICT_CUTOVER",
    "OPERATOR_INFO",
]

_TELEGRAM_LOG = Path("/opt/app/polyoracle/backend/_smoke_strict_logs/telegram_alerts.log")
_LAST_SENT: dict[str, float] = {}  # event_type → epoch seconds


def _config() -> tuple[str | None, str | None, int]:
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TELEGRAM_CHAT_ID"),
        int(os.environ.get("TELEGRAM_THROTTLE_S", "300")),
    )


def _log(msg: str) -> None:
    line = f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {msg}\n"
    try:
        _TELEGRAM_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_TELEGRAM_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass


def _format(event: EventType, message: str, context: dict | None) -> str:
    body = f"🤖 POLYORACLE — {event}\n\n{message}"
    if context:
        body += "\n\n" + "\n".join(f"  {k}: {v}" for k, v in context.items())
    body += f"\n\n⏱ {datetime.now(UTC).isoformat(timespec='seconds')}"
    return body


def notify(
    event: EventType,
    message: str,
    *,
    context: dict | None = None,
    force: bool = False,
) -> bool:
    """Send Telegram alert (or log if not configured / throttled).

    Returns True if actually sent, False if throttled/skipped.
    """
    token, chat_id, throttle_s = _config()
    now_ts = datetime.now(UTC).timestamp()

    # Throttle per event type unless forced
    if not force:
        last = _LAST_SENT.get(event, 0.0)
        if now_ts - last < throttle_s:
            _log(f"THROTTLED {event} ({int(now_ts - last)}s < {throttle_s}s): {message[:80]}")
            return False

    body = _format(event, message, context)

    # No telegram config → log-only mode
    if not token or not chat_id:
        _log(f"LOG_ONLY {event}: {message[:120]}")
        _LAST_SENT[event] = now_ts
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": body}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        _log(f"SENT {event}: {message[:80]}")
        _LAST_SENT[event] = now_ts
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        _log(f"FAILED {event}: {exc} | {message[:80]}")
        return False
