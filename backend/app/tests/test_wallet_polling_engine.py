"""Unit tests for WalletPollingEngine — dedupe, rate limit, lifecycle, resume."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

# Tests run inside the existing engine; the polling engine writes to
# ``data/wallet_polling_state.json`` by default. We reroute it via env to
# avoid clobbering production state.
os.environ["POLLING_STATE_FILE"] = str(Path(__file__).parent / "_polling_state_test.json")
os.environ["POLLING_INTERVAL_SECONDS"] = "1"  # tight loop for tests
os.environ["POLLING_RATE_LIMIT_CALLS_PER_SEC"] = "20"
os.environ["LIVE_ENABLED"] = "false"

from app.services.wallet_polling_engine import (  # noqa: E402
    PollingState,
    TokenBucket,
    WalletPollingEngine,
    reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_file = tmp_path / "polling_state.json"
    monkeypatch.setenv("POLLING_STATE_FILE", str(state_file))
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


def test_polling_state_round_trip(tmp_path: Path) -> None:
    state_file = tmp_path / "p.json"
    s = PollingState(last_seen_ts={}, state_file=state_file)
    s.update("0xabc", 100)
    s.update("0xabc", 50)  # going backwards is ignored
    s.update("0xDEF", 200)
    loaded = PollingState.load(state_file)
    assert loaded.last_seen_ts == {"0xabc": 100, "0xdef": 200}


@pytest.mark.asyncio
async def test_token_bucket_enforces_rate() -> None:
    bucket = TokenBucket(rate_per_sec=2.0, capacity=2)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # 5 calls at 2/s with capacity=2 burst → at least (5-2)/2 = 1.5s.
    assert elapsed >= 1.4


@pytest.mark.asyncio
async def test_polling_dedupes_already_seen_trades(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = WalletPollingEngine()
    addr = "0xabc"
    engine.state.update(addr, 100)  # already saw ts=100

    fetched: list[dict[str, Any]] = [
        {"timestamp": 50, "side": "BUY", "conditionId": "0xc", "outcomeIndex": 0, "size": 1, "price": 0.5},
        {"timestamp": 100, "side": "BUY", "conditionId": "0xc", "outcomeIndex": 0, "size": 1, "price": 0.5},
        {"timestamp": 150, "side": "BUY", "conditionId": "0xc", "outcomeIndex": 0, "size": 1, "price": 0.5},
        {"timestamp": 200, "side": "BUY", "conditionId": "0xc", "outcomeIndex": 0, "size": 1, "price": 0.5},
    ]

    class _FakeClient:
        async def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None):  # type: ignore[no-untyped-def]
            return httpx.Response(status_code=200, json=fetched)

    rows = await engine.fetch_recent_activity(_FakeClient(), addr, since_ts=100)  # type: ignore[arg-type]
    assert len(rows) == 2  # 150 + 200 only
    assert {int(r["timestamp"]) for r in rows} == {150, 200}


@pytest.mark.asyncio
async def test_polling_skips_invalid_sides_and_handles_500(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = WalletPollingEngine()

    class _FakeClient:
        def __init__(self):
            self.calls = 0

        async def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return httpx.Response(
                    status_code=200,
                    json=[
                        {"timestamp": 1, "side": "REDEEM"},
                        {"timestamp": 2, "side": "BUY", "conditionId": "0xc", "outcomeIndex": 0, "size": 1, "price": 0.5},
                    ],
                )
            return httpx.Response(status_code=500, json={})

    client = _FakeClient()
    rows = await engine.fetch_recent_activity(client, "0xa", since_ts=0)  # type: ignore[arg-type]
    assert len(rows) == 1 and rows[0]["side"] == "BUY"
    rows2 = await engine.fetch_recent_activity(client, "0xa", since_ts=0)  # type: ignore[arg-type]
    assert rows2 == []


@pytest.mark.asyncio
async def test_status_reflects_db_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = WalletPollingEngine()
    status = engine.status()
    assert "running" in status
    assert "wallets_polled_count" in status
    assert "trades_detected_total" in status
    assert "polling_errors" in status
    assert "interval_seconds" in status


def test_engine_refuses_to_start_with_live_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_ENABLED", "true")
    # Re-import settings cache by patching get_settings to honour the env var.
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_singleton_for_tests()
    with pytest.raises(PermissionError):
        WalletPollingEngine()
    # Restore for subsequent tests.
    monkeypatch.setenv("LIVE_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_load_cohort_filters_to_allowed_aggressive(tmp_path: Path) -> None:
    """Smoke-test the cohort loader with a synthetic universe csv."""
    csv_text = (
        "address,tier_v0_5_6,allowed_safe,allowed_aggressive,allowed_full_paper,composite_score,expected_value,ev_lower_bound\n"
        "0xa,ELITE_EV_VALIDATED,True,True,True,90,0.1,0.05\n"
        "0xb,STRONG_EV_VALIDATED,False,True,True,70,0.05,0.02\n"
        "0xc,SUSPICIOUS,False,False,False,40,0.0,-0.01\n"
    )
    universe = tmp_path / "validated_paper_universe_v0_5_6.csv"
    universe.write_text(csv_text, encoding="utf-8")
    cohort = WalletPollingEngine.load_cohort(universe)
    assert "0xa" in cohort and "0xb" in cohort
    assert "0xc" not in cohort
