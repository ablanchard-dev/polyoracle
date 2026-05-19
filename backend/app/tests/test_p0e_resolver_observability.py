"""P0-E (2026-05-19) tests — observability counters on market_metadata_resolver.

Read-only instrumentation. No logic change. Counters must accurately reflect
each branch in _resolve_inner :
- total_calls, blacklist_hits, lock_timeouts, cache_full_hits,
  cache_partial_misses, rate_limit_timeouts, gamma_fetches, gamma_errors,
  gamma_empty, clob_fetches, clob_errors, upsert_success/fail, result_*.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.services.market_metadata_resolver import (
    MarketMetadataResolver, ResolveResult,
)


def _make_resolver(gamma_doc=None, gamma_raises=False, orderbook=None, orderbook_raises=False):
    gamma = MagicMock()
    if gamma_raises:
        gamma.fetch_market_by_condition.side_effect = RuntimeError("gamma down")
    else:
        gamma.fetch_market_by_condition.return_value = gamma_doc
    clob = MagicMock()
    if orderbook_raises:
        clob.fetch_orderbook.side_effect = RuntimeError("clob down")
    else:
        clob.fetch_orderbook.return_value = orderbook or {}
    return MarketMetadataResolver(
        gamma_client=gamma, clob_client=clob,
        rate_per_sec=100.0, resolve_timeout_s=1.0,
    )


def test_stats_initial_state():
    r = _make_resolver(gamma_doc={})
    s = r.get_stats()
    assert s["total_calls"] == 0
    assert s["cache_full_hits"] == 0
    assert s["gamma_fetches"] == 0
    assert s["cache_hit_rate_pct"] == 0
    assert "blacklist_adds" in s
    assert "result_complete" in s


def test_stats_gamma_not_found_increments_blacklist_and_result():
    r = _make_resolver(gamma_doc=None)
    res = r.resolve("0xnonexistent")
    s = r.get_stats()
    assert s["total_calls"] == 1
    assert s["gamma_fetches"] == 1
    assert s["gamma_empty"] == 1
    assert s["blacklist_adds"] == 1
    assert s["result_not_found"] == 1
    assert res.status == "MARKET_METADATA_NOT_FOUND"


def test_stats_blacklist_hits_on_repeat_not_found():
    r = _make_resolver(gamma_doc=None)
    r.resolve("0xmarket_a")  # First call → blacklist add
    r.resolve("0xmarket_a")  # Second call → blacklist hit short-circuit
    r.resolve("0xmarket_a")  # Third → blacklist hit
    s = r.get_stats()
    assert s["total_calls"] == 3
    assert s["gamma_fetches"] == 1  # Only first call hit Gamma
    assert s["blacklist_hits"] == 2  # 2nd + 3rd
    assert s["blacklist_adds"] == 1
    assert s["result_not_found"] == 3
    assert s["blacklist_hit_rate_pct"] > 60


def test_stats_gamma_exception_increments_fetch_failed():
    r = _make_resolver(gamma_raises=True)
    res = r.resolve("0xmarket_b")
    s = r.get_stats()
    assert s["gamma_fetches"] == 1
    assert s["gamma_errors"] == 1
    assert s["result_fetch_failed"] == 1
    assert res.status == "MARKET_METADATA_FETCH_FAILED"


def test_stats_cache_hit_after_first_fetch():
    gamma_doc = {
        "conditionId": "0xmarket_c",
        "endDate": "2030-01-01T00:00:00Z",
        "category": "Test",
        "clobTokenIds": '["123","456"]',
        "outcomes": '["Yes","No"]',
    }
    r = _make_resolver(gamma_doc=gamma_doc, orderbook={"bids": [{"price": "0.5", "size": "100"}], "asks": [{"price": "0.51", "size": "100"}]})
    r.resolve("0xmarket_c")  # First → gamma fetch + cache populate
    r.resolve("0xmarket_c")  # Second → cache hit (static + dynamic both fresh)
    s = r.get_stats()
    assert s["total_calls"] == 2
    assert s["gamma_fetches"] == 1  # Only first
    assert s["cache_full_hits"] == 1  # Second was cache hit
    assert s["cache_partial_misses"] == 1  # First (no prior cache)


def test_stats_increment_thread_safe():
    """Multiple concurrent _bump_stat calls accumulate correctly."""
    import threading
    r = _make_resolver(gamma_doc=None)

    def hammer():
        for _ in range(100):
            r._bump_stat("total_calls")

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    s = r.get_stats()
    assert s["total_calls"] == 800  # 8 threads × 100 increments


def test_stats_derived_rates():
    r = _make_resolver(gamma_doc=None)
    for i in range(10):
        r.resolve(f"0xm{i:03d}")  # 10 unique → 10 fetches, 10 NOT_FOUND, 10 blacklist_adds
    s = r.get_stats()
    assert s["gamma_fetch_rate_pct"] == 100.0
    assert s["result_not_found_pct"] == 100.0
    assert s["cache_hit_rate_pct"] == 0.0


def test_get_stats_returns_snapshot_not_reference():
    """get_stats must return a copy — caller can't accidentally mutate
    the live counters."""
    r = _make_resolver(gamma_doc=None)
    r.resolve("0xa")
    snap = r.get_stats()
    snap["total_calls"] = 999  # Mutate the returned dict
    # Re-fetch fresh
    s2 = r.get_stats()
    assert s2["total_calls"] == 1  # Original unchanged
