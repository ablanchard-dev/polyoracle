"""Debug endpoints for memory profiling (2026-05-07).

Exposed at /debug/* — used to diagnose the 75 MB/min memory leak.
"""

from __future__ import annotations

import gc
import os
import tracemalloc
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/debug", tags=["debug"])

_BASELINE: tracemalloc.Snapshot | None = None


@router.post("/memory/snapshot")
def take_baseline() -> dict[str, Any]:
    """Take a tracemalloc baseline snapshot. Call once at start, then /memory/diff later."""
    global _BASELINE
    if not tracemalloc.is_tracing():
        raise HTTPException(500, detail="tracemalloc not enabled (start backend via dev_server.py)")
    gc.collect()
    _BASELINE = tracemalloc.take_snapshot()
    return {"baseline_taken": True, "n_traces": len(_BASELINE.traces)}


@router.get("/memory/diff")
def memory_diff(top_n: int = 20) -> dict[str, Any]:
    """Diff current vs baseline. Top N traces by memory growth."""
    if not tracemalloc.is_tracing():
        raise HTTPException(500, detail="tracemalloc not enabled")
    if _BASELINE is None:
        raise HTTPException(400, detail="no baseline — POST /debug/memory/snapshot first")
    gc.collect()
    current = tracemalloc.take_snapshot()
    stats = current.compare_to(_BASELINE, "lineno")
    out = []
    for stat in stats[:top_n]:
        out.append({
            "size_kb": round(stat.size / 1024, 1),
            "size_diff_kb": round(stat.size_diff / 1024, 1),
            "count": stat.count,
            "count_diff": stat.count_diff,
            "traceback": [str(f) for f in stat.traceback.format()],
        })
    return {"top": out}


@router.get("/memory/top")
def memory_top(top_n: int = 20, group_by: str = "lineno") -> dict[str, Any]:
    """Current top allocations (no baseline diff)."""
    if not tracemalloc.is_tracing():
        raise HTTPException(500, detail="tracemalloc not enabled")
    gc.collect()
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics(group_by)
    out = []
    for stat in stats[:top_n]:
        out.append({
            "size_kb": round(stat.size / 1024, 1),
            "count": stat.count,
            "traceback": [str(f) for f in stat.traceback.format()],
        })
    return {"top": out}


@router.get("/memory/process")
def memory_process() -> dict[str, Any]:
    """RSS / VMS of the current process (psutil-free, /proc-based)."""
    pid = os.getpid()
    try:
        with open(f"/proc/{pid}/status") as f:
            data = f.read()
    except FileNotFoundError:
        raise HTTPException(500, detail="/proc not available")
    out = {"pid": pid}
    for line in data.split("\n"):
        if line.startswith(("VmRSS:", "VmSize:", "VmPeak:", "VmSwap:")):
            k, v = line.split(":", 1)
            out[k] = v.strip()
    return out
