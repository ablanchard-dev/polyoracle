"""Unit tests for stream_pull_service — 2026-05-16.

Verifies the new batched polling architecture :
- filters trades by cohort (only ELITE wallets dispatched)
- deduplicates by transactionHash (no double-process)
- uses existing _raw_to_audit_input + _process_trade_in_session (no doctrine drift)
- coexists with per-wallet polling (counters mirrored)
"""
from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import MagicMock

import pytest

from app.services.stream_pull_service import StreamPullService


class _FakePollingEngine:
    """Minimal stand-in for WalletPollingEngine — only the surface we hit."""

    def __init__(self, cohort: list[str]):
        self._cohort = cohort
        self._trades_detected_session = 0
        self._paper_trades_session = 0
        self.processed_inputs: list[dict] = []

    @staticmethod
    def _raw_to_audit_input(raw: dict, address: str) -> dict:
        return {
            "trade_id": raw.get("transactionHash"),
            "address": address,
            "wallet": address,
            "market_id": raw.get("conditionId"),
            "outcome": raw.get("outcome"),
            "side": (raw.get("side") or "").upper(),
            "price": raw.get("price"),
            "size": raw.get("size"),
            "traded_at": str(raw.get("timestamp")),
            "data_source": "stream_pull_test",
        }

    def _process_trade_in_session(self, normalized: dict) -> dict:
        self.processed_inputs.append(normalized)
        return {"executed": True, "rejected": False}


def _fake_trade(tx_hash: str, wallet: str, ts: int = 1000) -> dict:
    return {
        "transactionHash": tx_hash,
        "proxyWallet": wallet,
        "conditionId": f"market-{tx_hash}",
        "outcome": "Yes",
        "side": "BUY",
        "size": 10.0,
        "price": 0.5,
        "timestamp": ts,
    }


def test_filter_cohort_only(monkeypatch):
    """Trade from wallet NOT in cohort must be ignored."""
    cohort = ["0xelite_a", "0xelite_b"]
    engine = _FakePollingEngine(cohort)
    svc = StreamPullService(engine, interval_s=1, limit=100)

    # 3 trades: 2 from cohort, 1 from outside.
    trades = [
        _fake_trade("tx1", "0xelite_a"),
        _fake_trade("tx2", "0xelite_b"),
        _fake_trade("tx3", "0xnot_in_cohort"),
    ]

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return trades

    class _FakeClient:
        async def get(self, *a, **kw): return _FakeResp()

    asyncio.run(svc._fetch_and_process(_FakeClient()))

    assert svc.trades_seen_total == 3
    assert svc.trades_matched_cohort == 2
    assert svc.trades_dispatched == 2
    assert len(engine.processed_inputs) == 2
    addresses = [p["address"] for p in engine.processed_inputs]
    assert "0xelite_a" in addresses
    assert "0xelite_b" in addresses
    assert "0xnot_in_cohort" not in addresses


def test_dedup_lru_skip_already_processed():
    """Same transactionHash in two consecutive fetches must not double-dispatch."""
    cohort = ["0xelite_a"]
    engine = _FakePollingEngine(cohort)
    svc = StreamPullService(engine, interval_s=1, limit=100)

    trades = [_fake_trade("tx1", "0xelite_a")]

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return trades

    class _FakeClient:
        async def get(self, *a, **kw): return _FakeResp()

    # First fetch — processes tx1
    asyncio.run(svc._fetch_and_process(_FakeClient()))
    assert svc.trades_dispatched == 1
    assert svc.trades_dedup_skipped == 0

    # Second fetch — same tx1, must skip
    asyncio.run(svc._fetch_and_process(_FakeClient()))
    assert svc.trades_dispatched == 1
    assert svc.trades_dedup_skipped == 1


def test_empty_cohort_no_dispatch():
    """If cohort is empty (e.g. boot before load_cohort), skip processing."""
    engine = _FakePollingEngine(cohort=[])
    svc = StreamPullService(engine, interval_s=1, limit=100)

    trades = [_fake_trade("tx1", "0xelite_a")]

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return trades

    class _FakeClient:
        async def get(self, *a, **kw): return _FakeResp()

    asyncio.run(svc._fetch_and_process(_FakeClient()))
    assert svc.trades_dispatched == 0
    assert svc.trades_matched_cohort == 0


def test_counters_mirror_polling_engine():
    """trades_detected_session + paper_trades_session must increment on engine."""
    cohort = ["0xelite_a"]
    engine = _FakePollingEngine(cohort)
    svc = StreamPullService(engine, interval_s=1, limit=100)

    trades = [_fake_trade("tx1", "0xelite_a"), _fake_trade("tx2", "0xelite_a")]

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return trades

    class _FakeClient:
        async def get(self, *a, **kw): return _FakeResp()

    asyncio.run(svc._fetch_and_process(_FakeClient()))
    assert engine._trades_detected_session == 2
    assert engine._paper_trades_session == 2  # _FakePollingEngine returns executed=True


def test_case_insensitive_wallet_match():
    """Cohort uses lowercase, but trade wallet may be mixed-case."""
    cohort = ["0xelite_a"]
    engine = _FakePollingEngine(cohort)
    svc = StreamPullService(engine, interval_s=1, limit=100)

    trade = _fake_trade("tx1", "0xELITE_A")  # uppercase

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [trade]

    class _FakeClient:
        async def get(self, *a, **kw): return _FakeResp()

    asyncio.run(svc._fetch_and_process(_FakeClient()))
    assert svc.trades_dispatched == 1


def test_trade_missing_id_is_skipped():
    """Trade without transactionHash or id must be skipped (no infinite re-process)."""
    cohort = ["0xelite_a"]
    engine = _FakePollingEngine(cohort)
    svc = StreamPullService(engine, interval_s=1, limit=100)

    trade = _fake_trade("tx1", "0xelite_a")
    trade["transactionHash"] = None
    trade["id"] = None

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [trade]

    class _FakeClient:
        async def get(self, *a, **kw): return _FakeResp()

    asyncio.run(svc._fetch_and_process(_FakeClient()))
    assert svc.trades_dispatched == 0
