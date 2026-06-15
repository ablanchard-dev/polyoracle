"""Polling error classifier (POLYORACLE 2026-05-11, P0.4 review Round 4).

Le compteur global `polling_errors` mélange tout : CLOB 404 (= noise attendu),
5xx Polymarket transient (= retried, OK), 429 rate limit (= signal critique),
ConnectError (= signal partiel), parse errors (= bug). C'est trompeur :
"+1138 errors" peut être 99% noise OR 99% critique → grosse différence
opérationnelle.

Ce service lit `backend.dev.err.log` (output `stderr` du backend) + le
counter `polling_errors` de BotLoopState, et classifie en 3 buckets :

  - NOISE      : CLOB 404 / orderbook not found (attendu sur crypto 5min)
  - TRANSIENT  : 5xx Polymarket, ConnectError, ChunkedEncodingError (retried)
  - CRITICAL   : 429 rate limit, auth fail, parse error, DB write fail (action)

Expose via GET `/observability/polling-errors`. Aucune mutation, aucun impact
runtime. Lecture seule du log + DB.

Verbatim review :
> "Si `429 > 100/h` ou `critical_errors > 200/h`, on déclenche backoff
>  exponentiel, pause rate bump, alert Telegram, pas de live tant que rouge."
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_PATH = _BACKEND_ROOT / "_smoke_strict_logs" / "backend.dev.err.log"
FALLBACK_LOG_PATH = _BACKEND_ROOT / "backend.dev.err.log"

# Pattern catalog — order matters: most-specific first
PATTERN_MAP: list[tuple[str, re.Pattern[str]]] = [
    # NOISE patterns (= comportement attendu, pas action)
    ("CLOB_404_NOISE", re.compile(r"CLOB orderbook failed.*404")),
    # CRITICAL patterns
    ("RATE_LIMIT_429", re.compile(r"\b429\b|HTTP\s*429|Too Many Requests")),
    ("AUTH_FAIL", re.compile(r"\b401\b|\b403\b|Unauthorized|Forbidden|signature.*invalid")),
    ("PARSE_FAIL", re.compile(r"json.decoder|JSONDecodeError|ValidationError")),
    ("DB_WRITE_FAIL", re.compile(r"OperationalError|DatabaseError|IntegrityError")),
    # TRANSIENT patterns
    ("HTTP_5XX", re.compile(r"\b50[0-9]\b|HTTP\s*50[0-9]|Internal Server|Bad Gateway|Service Unavailable|Gateway Timeout")),
    ("CONNECT_ERROR", re.compile(r"ConnectError|ConnectionError|ConnectionRefused|RemoteDisconnected|ChunkedEncodingError|ProtocolError")),
    ("TIMEOUT", re.compile(r"TimeoutError|ReadTimeout|ConnectTimeout|asyncio.TimeoutError")),
]

NOISE_CODES = frozenset({"CLOB_404_NOISE"})
TRANSIENT_CODES = frozenset({"HTTP_5XX", "CONNECT_ERROR", "TIMEOUT"})
CRITICAL_CODES = frozenset({"RATE_LIMIT_429", "AUTH_FAIL", "PARSE_FAIL", "DB_WRITE_FAIL"})

# Alert thresholds (per hour)
CRITICAL_ALERT_THRESHOLD_PER_HOUR = 200
RATE_LIMIT_ALERT_THRESHOLD_PER_HOUR = 100


@dataclass
class ClassificationReport:
    window_hours: float
    total_log_lines_scanned: int
    counts_by_pattern: dict[str, int]
    counts_by_bucket: dict[str, int]  # noise / transient / critical
    rate_per_hour_by_bucket: dict[str, float]
    alerts: list[str] = field(default_factory=list)
    signal_loss_estimated_pct: float | None = None
    log_path: str = ""
    log_size_bytes: int = 0


def _classify_line(line: str) -> str | None:
    """Returns the pattern code matched, or None if not a recognized error line."""
    for code, pattern in PATTERN_MAP:
        if pattern.search(line):
            return code
    return None


def _bucket_for(code: str) -> str:
    if code in NOISE_CODES:
        return "noise"
    if code in CRITICAL_CODES:
        return "critical"
    if code in TRANSIENT_CODES:
        return "transient"
    return "unknown"


def _read_log_tail(path: Path, max_bytes: int = 10_000_000) -> tuple[list[str], int]:
    """Read up to max_bytes from end of file. Returns (lines, total_size).
    For very large logs, only last N MB are scanned."""
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(-max_bytes, 2)
            f.readline()  # skip partial line
        try:
            content = f.read().decode("utf-8", errors="ignore")
        except Exception:
            return [], size
    return content.splitlines(), size


def classify_polling_errors(
    *, log_path: Path | None = None, window_hours: float = 6.0,
) -> ClassificationReport:
    """Classify polling errors from the backend stderr log.

    Note: the current log doesn't have per-line timestamps (uvicorn's default
    format), so window_hours is an approximation based on log volume. Future
    upgrade: structured logging with explicit timestamps per line.
    """
    path = log_path or DEFAULT_LOG_PATH
    if not path.exists() and FALLBACK_LOG_PATH.exists():
        path = FALLBACK_LOG_PATH
    lines, size = _read_log_tail(path)
    counts: Counter[str] = Counter()
    for line in lines:
        code = _classify_line(line)
        if code:
            counts[code] += 1

    buckets: Counter[str] = Counter()
    for code, n in counts.items():
        buckets[_bucket_for(code)] += n

    # Rate per hour (approximation if no timestamps)
    rate_per_hour = {
        bucket: round(n / max(window_hours, 0.01), 2)
        for bucket, n in buckets.items()
    }

    alerts: list[str] = []
    if rate_per_hour.get("critical", 0) > CRITICAL_ALERT_THRESHOLD_PER_HOUR:
        alerts.append(
            f"CRITICAL: {rate_per_hour['critical']:.0f} critical errors/h "
            f"> {CRITICAL_ALERT_THRESHOLD_PER_HOUR}"
        )
    rate_429 = counts.get("RATE_LIMIT_429", 0) / max(window_hours, 0.01)
    if rate_429 > RATE_LIMIT_ALERT_THRESHOLD_PER_HOUR:
        alerts.append(
            f"RATE_LIMIT: 429 {rate_429:.0f}/h > {RATE_LIMIT_ALERT_THRESHOLD_PER_HOUR} — "
            "DO NOT bump rate, add backoff first"
        )

    # Estimated signal loss = (critical + ~5% of transient) / total polls assumed
    # Polling = 465 wallets / 30s = 93/min = ~5,580/h
    critical_count = buckets.get("critical", 0)
    transient_count = buckets.get("transient", 0)
    estimated_lost = critical_count + 0.05 * transient_count
    polls_assumed = 5580 * window_hours
    signal_loss_pct = (estimated_lost / max(polls_assumed, 1)) * 100

    return ClassificationReport(
        window_hours=window_hours,
        total_log_lines_scanned=len(lines),
        counts_by_pattern=dict(counts),
        counts_by_bucket=dict(buckets),
        rate_per_hour_by_bucket=rate_per_hour,
        alerts=alerts,
        signal_loss_estimated_pct=round(signal_loss_pct, 2),
        log_path=str(path),
        log_size_bytes=size,
    )


def classify_payload(window_hours: float = 6.0) -> dict[str, Any]:
    """For FastAPI route."""
    return asdict(classify_polling_errors(window_hours=window_hours))
