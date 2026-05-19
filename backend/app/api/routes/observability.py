"""v0.7.8 Phase 8 — Observability endpoints for the UI cockpit.

Exposes the latency tracker, adaptive close scheduler state, and
resolver cache stats so the Next.js frontend can build a real-time
dashboard.

All endpoints are read-only and lightweight (no DB queries, just
in-memory state). Safe to poll from the UI every 1-5s.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/workers")
def workers_status() -> dict[str, Any]:
    """P0-B workers pool stats — polls per lane, errors, queue size.

    Returns the live WorkerPool.stats. Allows operator to verify WARM/COLD
    wallets are actually being polled (item 3 of ALL-ELITES-SERVED audit)."""
    try:
        from app.services.wallet_polling_engine import WalletPollingEngine
        from app.services import polling_workers as _pw
        eng = WalletPollingEngine._instance
        if eng is None:
            return {"error": "engine instance is None"}
        pool = getattr(eng, "_worker_pool", None)
        if pool is None:
            return {
                "workers_enabled_flag": _pw.is_enabled(),
                "pool": None,
                "note": "workers not active (flag off OR engine on legacy loop)",
            }
        # WRR scheduler has .scheduler attr; legacy pool has .queue
        sched_or_queue = getattr(pool, "scheduler", None) or getattr(pool, "queue", None)
        result = {
            "workers_enabled_flag": _pw.is_enabled(),
            "scheduler_type": "WRR" if hasattr(pool, "scheduler") else "LEGACY_PRIORITY",
            "n_workers": pool.n_workers,
            "queue_size": sched_or_queue.size() if sched_or_queue else None,
            "polls_done": pool.stats.get("polls_done", 0),
            "errors": pool.stats.get("errors", 0),
            "by_lane": pool.stats.get("by_lane", {}),
            "cohort_size": len(eng._cohort or []),
            "lane_distribution": {
                lane: sum(1 for l in eng._wallet_lanes.values() if l == lane)
                for lane in ("HOT", "WARM", "COLD")
            } if hasattr(eng, "_wallet_lanes") else None,
        }
        # WRR-specific stats
        if hasattr(sched_or_queue, "size_by_lane"):
            result["queue_size_by_lane"] = sched_or_queue.size_by_lane()
        if hasattr(sched_or_queue, "served_by_lane"):
            result["served_by_lane"] = sched_or_queue.served_by_lane()
        # P0-C waterfall : compute average phase ms per poll
        phases = pool.stats.get("phase_total_s")
        polls = pool.stats.get("polls_done", 0) or 1
        if phases:
            result["phase_avg_ms"] = {k: round(1000*v/polls, 2) for k, v in phases.items()}
            result["phase_total_s"] = {k: round(v, 2) for k, v in phases.items()}
        with_t = pool.stats.get("polls_with_trade", 0)
        without_t = pool.stats.get("polls_without_trade", 0)
        if with_t > 0:
            result["poll_func_avg_ms_with_trade"] = round(1000*pool.stats.get("poll_func_s_with_trade",0)/with_t, 2)
        if without_t > 0:
            result["poll_func_avg_ms_without_trade"] = round(1000*pool.stats.get("poll_func_s_without_trade",0)/without_t, 2)
        result["polls_with_trade"] = with_t
        result["polls_without_trade"] = without_t
        result["hit_rate_pct"] = round(100*with_t/(with_t+without_t), 2) if (with_t+without_t) else 0
        # P0-D 2026-05-19 — two-stage audit queue stats
        from app.services import polling_two_stage as _pts
        audit_q = getattr(eng, "_audit_queue", None)
        audit_p = getattr(eng, "_audit_pool", None)
        result["two_stage_enabled"] = _pts.is_enabled()
        if audit_q is not None:
            result["audit_queue"] = {
                "qsize": audit_q.qsize(),
                "maxsize": audit_q.maxsize,
                "kill_threshold": audit_q.kill_threshold,
                "enqueued": audit_q.stats.get("enqueued", 0),
                "dropped_full": audit_q.stats.get("dropped_full", 0),
                "dequeued": audit_q.stats.get("dequeued", 0),
                "kill_threshold_hits": audit_q.stats.get("kill_threshold_hits", 0),
            }
        if audit_p is not None:
            result["audit_pool"] = {
                "n_workers": audit_p.n_workers,
                "processed": audit_p.stats.get("processed", 0),
                "errors": audit_p.stats.get("errors", 0),
                "phase_total_s": audit_p.stats.get("phase_total_s", {}),
            }
        return result
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.get("/stream-pull")
def stream_pull_status() -> dict[str, Any]:
    """Stream-pull service runtime counters. Returns {enabled: False, ...}
    if service not initialized (e.g. STREAM_PULL_ENABLED=false at boot)."""
    try:
        from app.services.stream_pull_service import (
            STREAM_PULL_ENABLED,
            get_stream_pull_service,
        )
        svc = get_stream_pull_service()
        if svc is None:
            return {
                "enabled_flag": STREAM_PULL_ENABLED,
                "instance": None,
                "note": "service not initialized (singleton is None)",
            }
        # Get cohort_size live for context
        try:
            cohort_size = len(svc.polling_engine._cohort or [])
        except Exception:
            cohort_size = -1
        return {
            "enabled_flag": STREAM_PULL_ENABLED,
            "running": svc.is_running(),
            "interval_s": svc.interval_s,
            "limit": svc.limit,
            "max_trade_age_s": svc.max_trade_age_s,
            "cohort_size": cohort_size,
            "cycles_completed": svc.cycles_completed,
            "cycles_failed": svc.cycles_failed,
            "trades_seen_total": svc.trades_seen_total,
            "trades_matched_cohort": svc.trades_matched_cohort,
            "trades_dedup_skipped": svc.trades_dedup_skipped,
            "trades_skipped_too_old": svc.trades_skipped_too_old,
            "last_skipped_too_old_age_s": svc.last_skipped_too_old_age_s,
            "trades_dispatched": svc.trades_dispatched,
            "paper_executed": svc.paper_executed,
            "last_cycle_at": svc.last_cycle_at,
            "last_error": svc.last_error,
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.get("/utilization")
def utilization_status(
    window_hours: float = 24.0,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """M5 throughput classifier (A-INSTR Phase A — 2026-05-11).

    Returns max_pos_utilization avg/p95, slot_block_rate, capital_utilization,
    fresh_signal_rate, and the 5-bound classification (slot/capital/opportunity/
    latency/risk-gate) so the operator knows which Phase C lever to pull.
    Default window 24h; pass ?window_hours=N to widen/narrow."""
    from app.services.throughput_classifier import utilization_payload
    return utilization_payload(session, window_hours=window_hours)


@router.get("/polling-errors")
def polling_errors_classified(window_hours: float = 6.0) -> dict[str, Any]:
    """P0.4 polling error classifier (review Round 4 — 2026-05-11).

    Classifies errors from backend.dev.err.log into noise / transient / critical
    buckets. Surfaces signal_loss_estimated_pct + alerts for rate-limit /
    critical thresholds. READ-ONLY (no runtime impact)."""
    from app.services.polling_error_classifier import classify_payload
    return classify_payload(window_hours=window_hours)


@router.get("/baseline-info")
def baseline_info() -> dict[str, Any]:
    """P0.1 effective baseline T0 (post category_resolver fix — 2026-05-11).

    Returns the EFFECTIVE_BASELINE_T0 (=2026-05-11T13:21:33Z) and diagnostic
    period (01:54-13:21Z) info. Phase B metrics MUST be computed from this T0,
    NOT from strict_cutover_at."""
    from app.services.baseline_constants import baseline_info as _baseline_info
    return _baseline_info()


@router.get("/latency")
def latency_status() -> dict[str, Any]:
    """Per-path latency p50/p95/max + breach flags.

    Used by the UI cockpit's Latency dashboard. Shows whether each
    pipeline step is within its budget (Vision Lock §4)."""
    from app.services.latency_tracker import (
        LATENCY_BUDGET_MS,
        get_tracker,
    )
    tracker = get_tracker()
    paths_status = tracker.all_paths_status()
    return {
        "paths": {
            name: {
                **stats,
                "budget_ms": LATENCY_BUDGET_MS.get(name),
                "ratio": (
                    stats["p95"] / LATENCY_BUDGET_MS[name]
                    if name in LATENCY_BUDGET_MS and LATENCY_BUDGET_MS[name] > 0
                    else None
                ),
            }
            for name, stats in paths_status.items()
        },
        "budgets": LATENCY_BUDGET_MS,
    }


@router.get("/latency/report")
def latency_report() -> dict[str, str]:
    """Markdown latency report — for the UI's Daily Report tab."""
    from app.services.latency_tracker import daily_report
    return {"report_md": daily_report()}


@router.get("/scheduler")
def scheduler_status() -> dict[str, Any]:
    """Adaptive close scheduler stats: registered positions, heap size."""
    from app.services.adaptive_close_scheduler import (
        BUCKET_CHECK_INTERVAL_S,
        get_scheduler,
    )
    scheduler = get_scheduler()
    return {
        "registered_positions": scheduler.known_positions_count(),
        "heap_size": scheduler.heap_size(),
        "bucket_intervals_s": BUCKET_CHECK_INTERVAL_S,
    }


@router.get("/resolver")
def resolver_status() -> dict[str, Any]:
    """Market metadata resolver cache + stats. P0-E observability."""
    from app.services.market_metadata_resolver import (
        DYNAMIC_DATA_TTL_S,
        NOT_FOUND_BLACKLIST_TTL_S,
        STATIC_METADATA_TTL_S,
        RESOLVE_TIMEOUT_S,
        get_resolver,
    )
    resolver = get_resolver()
    stats = resolver.get_stats() if hasattr(resolver, "get_stats") else {}
    return {
        "static_cache_size": len(resolver._static_cache),
        "dynamic_cache_size": len(resolver._dynamic_cache),
        "not_found_blacklist_size": len(resolver._not_found),
        "ttl": {
            "static_s": STATIC_METADATA_TTL_S,
            "dynamic_s": DYNAMIC_DATA_TTL_S,
            "not_found_s": NOT_FOUND_BLACKLIST_TTL_S,
            "resolve_timeout_s": RESOLVE_TIMEOUT_S,
        },
        "stats": stats,
    }


@router.post("/kill-switch-flatten")
def kill_switch_flatten() -> dict[str, Any]:
    """KILL SWITCH — flatten all open paper positions immediately.

    For paper mode: closes all open positions at their entry price
    (zero realized PnL). For live mode (Phase 7): would submit market
    orders to flatten via CLOB.

    Used by the UI's red 'KILL SWITCH' button. Idempotent — safe to
    call multiple times.
    """
    from app.database import engine
    from app.models.trade import PaperTrade
    from app.services.paper_trading_engine import (
        CLOSE_REASON_MANUAL,
        _close_paper_with_reason,
    )
    from sqlmodel import Session, select

    closed_ids = []
    with Session(engine) as session:
        rows = list(session.exec(
            select(PaperTrade).where(PaperTrade.status == "open")
        ))
        for trade in rows:
            try:
                # Close at entry price = zero PnL = flatten safely
                _close_paper_with_reason(
                    session, trade,
                    reason=CLOSE_REASON_MANUAL,
                    exit_price=trade.average_price,
                )
                closed_ids.append(trade.id)
            except Exception as e:
                pass  # don't block other closes if one fails
    return {
        "closed_count": len(closed_ids),
        "closed_ids": closed_ids,
        "message": f"Flattened {len(closed_ids)} open paper positions",
    }
