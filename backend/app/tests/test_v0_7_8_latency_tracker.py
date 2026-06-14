"""v0.7.8 Phase 6 — Latency tracker tests."""

from __future__ import annotations

import time

import pytest

from app.services.latency_tracker import (
    BREACH_MULTIPLIER,
    LATENCY_BUDGET_MS,
    RING_BUFFER_SIZE,
    LatencyTracker,
    daily_report,
    get_tracker,
    reset_tracker_for_tests,
    track_latency,
)


# ---------------- 1 — record + quantiles ----------------


def test_record_returns_correct_quantiles():
    """Record 100 samples (10..1000ms by 10) and verify p50/p95/max."""
    t = LatencyTracker()
    for i in range(1, 101):
        t.record("test_path", float(i * 10))
    q = t.quantiles("test_path")
    assert q["n"] == 100
    assert q["p50"] == pytest.approx(505.0, abs=10)
    assert q["p95"] == pytest.approx(955.0, abs=20)
    assert q["max"] == 1000.0


def test_quantiles_handles_empty_path():
    t = LatencyTracker()
    q = t.quantiles("never_recorded")
    assert q["n"] == 0
    assert q["p50"] == 0.0


# ---------------- 2 — context manager wraps timing ----------------


def test_track_latency_context_manager_records():
    reset_tracker_for_tests()
    # Sleep well above time.monotonic()'s Windows granularity (~15.6 ms via
    # GetTickCount64). At 10 ms both monotonic reads can land in the same tick
    # → elapsed reads 0 ms → flaky `max >= 5.0` (~1/5 on Windows; Linux CI is
    # nanosecond-resolution and never hits it). 50 ms clears the tick reliably.
    with track_latency("ctx_test"):
        time.sleep(0.05)  # 50 ms — above Windows monotonic granularity
    q = get_tracker().quantiles("ctx_test")
    assert q["n"] == 1
    assert q["max"] >= 5.0  # at least 5ms recorded


# ---------------- 3 — ring buffer enforces max size ----------------


def test_ring_buffer_caps_at_size():
    t = LatencyTracker()
    for i in range(RING_BUFFER_SIZE + 100):
        t.record("ring_test", float(i))
    q = t.quantiles("ring_test")
    assert q["n"] == RING_BUFFER_SIZE  # extra samples dropped


# ---------------- 4 — breach flag fires above 2× budget ----------------


def test_breach_flag_set_when_p95_exceeds_2x_budget():
    t = LatencyTracker()
    budget = LATENCY_BUDGET_MS.get("audit_trade", 500)
    breach_threshold = budget * BREACH_MULTIPLIER  # 1000ms

    # Record 100 samples all at 1500ms (well above breach)
    for _ in range(100):
        t.record("audit_trade", 1500.0)
    q = t.quantiles("audit_trade")
    assert q["breach"] is True
    assert q["p95"] >= breach_threshold


def test_breach_flag_not_set_under_budget():
    t = LatencyTracker()
    budget = LATENCY_BUDGET_MS.get("audit_trade", 500)
    # Record 100 samples at 100ms (well under budget)
    for _ in range(100):
        t.record("audit_trade", 100.0)
    q = t.quantiles("audit_trade")
    assert q["breach"] is False


# ---------------- 5 — invalid samples ignored ----------------


def test_negative_elapsed_ignored():
    t = LatencyTracker()
    t.record("test", -100.0)
    q = t.quantiles("test")
    assert q["n"] == 0


# ---------------- 6 — all_paths_status aggregates ----------------


def test_all_paths_status_returns_all_recorded():
    t = LatencyTracker()
    t.record("path_a", 10.0)
    t.record("path_b", 20.0)
    t.record("path_a", 15.0)
    status = t.all_paths_status()
    assert "path_a" in status
    assert "path_b" in status
    assert status["path_a"]["n"] == 2
    assert status["path_b"]["n"] == 1


# ---------------- 7 — reset clears samples ----------------


def test_reset_clears_path():
    t = LatencyTracker()
    t.record("test", 100.0)
    t.reset("test")
    q = t.quantiles("test")
    assert q["n"] == 0


def test_reset_all_clears_everything():
    t = LatencyTracker()
    t.record("a", 1)
    t.record("b", 2)
    t.reset()  # no path → reset all
    assert t.all_paths_status() == {}


# ---------------- 8 — daily_report markdown ----------------


def test_daily_report_returns_markdown():
    reset_tracker_for_tests()
    tracker = get_tracker()
    tracker.record("audit_trade", 250.0)
    tracker.record("audit_trade", 300.0)
    report = daily_report()
    assert "# Latency Report" in report
    assert "audit_trade" in report
    assert "p50" in report.lower() or "P50" in report or "p50 (ms)" in report.lower()


def test_daily_report_handles_empty():
    reset_tracker_for_tests()
    report = daily_report()
    assert "no samples" in report.lower() or "Latency Report" in report


# ---------------- 9 — singleton across get_tracker calls ----------------


def test_singleton_persists():
    reset_tracker_for_tests()
    t1 = get_tracker()
    t1.record("test_singleton", 50.0)
    t2 = get_tracker()
    assert t1 is t2
    q = t2.quantiles("test_singleton")
    assert q["n"] == 1


# ---------------- 10 — budget defined for documented paths ----------------


def test_all_documented_paths_have_budgets():
    """Vision Lock §4 documents these paths. Each must have a budget."""
    required = [
        "polling_fetch_per_wallet",
        "audit_trade",
        "cluster_check",
        "allocator_evaluate",
        "paper_open_total",
        "resolver_resolve",
        "close_loop_iteration",
        "live_order_submit",
    ]
    for path in required:
        assert path in LATENCY_BUDGET_MS, f"Missing budget for path: {path}"
        assert LATENCY_BUDGET_MS[path] > 0, f"Budget must be positive: {path}"
