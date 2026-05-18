"""P0-B (2026-05-18) tests — polling delay observer is log-only, flag-gated,
and never raises into the calling pipeline.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import polling_delay_observer as pdo


@pytest.fixture
def temp_obs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pdo, "_OBS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def enable_obs(monkeypatch):
    monkeypatch.setenv("POLLING_DELAY_OBS_ENABLED", "true")


@pytest.fixture
def disable_obs(monkeypatch):
    monkeypatch.setenv("POLLING_DELAY_OBS_ENABLED", "false")


def _raw_sample() -> dict:
    return {
        "timestamp": 1716050000,
        "wallet_address": "0xabc123",
        "market_id": "0xm1",
        "side": "BUY",
        "outcome": "Yes",
        "price": 0.5,
    }


def _stub_audit_record():
    class A:
        decision = "PAPER_TRADE"
        copy_delay_seconds = 12.0
        copyable_edge = 87.5
        orderbook_quality = "GOOD"
        trade_quality_score = 85.0
        wallet_tier = "ELITE_GOLD"
        notional_usd = 1553.78
        spread = 0.02
        market_liquidity_score = 0.85
    return A()


def _stub_wallet_record(wr=0.97, status="ELITE"):
    class W:
        candidate_status = status
        resolved_market_win_rate = wr
    return W()


def test_disabled_returns_none_creates_no_file(temp_obs_dir, disable_obs):
    obs = pdo.start_observation(_raw_sample())
    pdo.finalize_from_result(obs, {"executed": True})
    assert obs is None
    assert list(temp_obs_dir.iterdir()) == []


def test_enabled_with_none_raw_returns_none(temp_obs_dir, enable_obs):
    assert pdo.start_observation(None) is None


def test_enabled_invalid_timestamp_does_not_raise(temp_obs_dir, enable_obs):
    raw = {**_raw_sample(), "timestamp": "not-a-number"}
    obs = pdo.start_observation(raw)
    assert obs is not None
    assert obs["source_trade_ts_unix"] == 0


def test_enabled_opened_outcome_writes_jsonl(temp_obs_dir, enable_obs):
    obs = pdo.start_observation(_raw_sample())
    pdo.attach_audit(obs, _stub_audit_record())
    pdo.attach_wallet(obs, _stub_wallet_record(wr=0.97))
    pdo.attach_lane(obs, "HOT")
    pdo.finalize_from_result(obs, {"executed": True, "audit_id": "audit-1"})

    files = list(temp_obs_dir.glob("delay_obs_*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text().strip().splitlines()[0]
    rec = json.loads(line)
    assert rec["decision_outcome"] == "OPENED"
    assert rec["wallet_address"] == "0xabc123"
    assert rec["wallet_bucket"] == "SILVER"  # wr=0.97 between 0.95 and 0.99
    assert rec["candidate_status"] == "ELITE"
    assert rec["lane"] == "HOT"
    assert rec["orderbook_quality"] == "GOOD"
    assert rec["copyable_edge"] == 87.5
    assert rec["notional_usd"] == 1553.78
    assert rec["delay_total_ms"] >= 0
    assert "delay_source_to_seen_ms" in rec
    assert rec["reject_reason_code"] is None


def test_enabled_rejected_outcome_writes_reject_reason(temp_obs_dir, enable_obs):
    obs = pdo.start_observation(_raw_sample())
    pdo.finalize_from_result(obs, {
        "executed": False, "rejected": True,
        "reason_code": "STALE_SIGNAL_BACKFILL",
        "reason": "STALE_SIGNAL_BACKFILL:stale:120s",
    })

    files = list(temp_obs_dir.glob("delay_obs_*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text().strip().splitlines()[0])
    assert rec["decision_outcome"] == "REJECTED"
    assert rec["reject_reason_code"] == "STALE_SIGNAL_BACKFILL"
    assert rec["paper_opened_at_ms"] is None


def test_buckets_classification(temp_obs_dir, enable_obs):
    cases = [
        (0.999, "GOLD"),
        (0.97, "SILVER"),
        (0.92, "BRONZE"),
        (0.80, "REG"),
    ]
    for wr, expected in cases:
        obs = pdo.start_observation(_raw_sample())
        pdo.attach_wallet(obs, _stub_wallet_record(wr=wr))
        pdo.finalize_from_result(obs, {"executed": True})
    files = list(temp_obs_dir.glob("delay_obs_*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().strip().splitlines()
    buckets = [json.loads(l)["wallet_bucket"] for l in lines]
    assert buckets == ["GOLD", "SILVER", "BRONZE", "REG"]


def test_active_contextvar_attach_path(temp_obs_dir, enable_obs):
    """In-pipeline hook: start_observation sets contextvar; attach_audit_active
    finds it and decorates without explicit obs reference."""
    obs = pdo.start_observation(_raw_sample())
    assert obs is not None
    pdo.attach_audit_active(_stub_audit_record())
    pdo.attach_wallet_active(_stub_wallet_record())
    pdo.finalize_from_result(obs, {"executed": True})
    files = list(temp_obs_dir.glob("delay_obs_*.jsonl"))
    rec = json.loads(files[0].read_text().strip())
    assert rec["copyable_edge"] == 87.5
    assert rec["wallet_bucket"] == "SILVER"


def test_attach_active_noop_when_disabled(temp_obs_dir, disable_obs):
    """If flag off, attach_*_active functions must silently no-op."""
    pdo.start_observation(_raw_sample())  # returns None, sets contextvar to None
    pdo.attach_audit_active(_stub_audit_record())  # should not crash
    pdo.attach_wallet_active(_stub_wallet_record())  # should not crash
    pdo.finalize_from_result(None, {"executed": True})
    assert list(temp_obs_dir.iterdir()) == []


def test_finalize_swallows_errors(temp_obs_dir, enable_obs, caplog):
    """Even with a corrupt obs dict, finalize must not raise."""
    bad_obs = {"source_trade_ts_unix": "broken", "first_seen_at_ms": None}
    pdo.finalize_from_result(bad_obs, {"executed": True})


def test_attach_audit_swallows_attribute_errors(temp_obs_dir, enable_obs):
    """attach_audit on object without expected attrs must not raise."""
    obs = pdo.start_observation(_raw_sample())

    class Empty: pass
    pdo.attach_audit(obs, Empty())
    pdo.attach_wallet(obs, Empty())
    pdo.finalize_from_result(obs, {"executed": True})
    files = list(temp_obs_dir.glob("delay_obs_*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text().strip())
    # Outcome captured even though attach calls were partial/no-op
    assert rec["decision_outcome"] == "OPENED"


def test_is_enabled_flag_variants(monkeypatch):
    for val, expected in [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("", False), ("no", False),
    ]:
        monkeypatch.setenv("POLLING_DELAY_OBS_ENABLED", val)
        assert pdo.is_enabled() is expected, f"failed for {val!r}"
    monkeypatch.delenv("POLLING_DELAY_OBS_ENABLED", raising=False)
    assert pdo.is_enabled() is False  # default false
