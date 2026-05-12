"""D7 single-instance lock (Phase D 2026-05-11).

Prevents two backend processes from running concurrently against the same
DB (would cause double trades + corrupted polling state). Uses an OS-level
fcntl flock on a sentinel file. Released automatically on process exit.

Usage (in app/main.py @ startup):

    from app.services.single_instance_lock import acquire_lock_or_die

    @app.on_event("startup")
    def startup():
        acquire_lock_or_die()
        ...

If another instance holds the lock, this raises SystemExit(1) with a
clear message (logs to stderr).
"""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path

DEFAULT_LOCK_PATH = Path("/tmp/polyoracle.lock")


class _LockHandle:
    """Module-level singleton — keeps fd open for the lifetime of the process."""
    fd: int | None = None
    path: Path | None = None


def acquire_lock_or_die(path: Path | str = DEFAULT_LOCK_PATH) -> None:
    """Acquire an exclusive flock or exit. Idempotent within the same process."""
    if _LockHandle.fd is not None:
        return  # already held by this process
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        # Read PID of holder for diagnostic
        try:
            holder = p.read_text().strip()
        except Exception:
            holder = "unknown"
        sys.stderr.write(
            f"\n[FATAL] POLYORACLE single-instance lock {p} is held by PID={holder}.\n"
            f"Refusing to start a second backend (would double-trade + corrupt polling state).\n"
            f"Stop the other instance first: kill {holder} && rm {p}\n\n"
        )
        raise SystemExit(1)
    # Write own PID for diagnostics
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _LockHandle.fd = fd
    _LockHandle.path = p


def release_lock() -> None:
    """Explicit release (mostly for tests). Lock is also auto-released on exit."""
    if _LockHandle.fd is None:
        return
    try:
        fcntl.flock(_LockHandle.fd, fcntl.LOCK_UN)
    finally:
        os.close(_LockHandle.fd)
        _LockHandle.fd = None
        _LockHandle.path = None
