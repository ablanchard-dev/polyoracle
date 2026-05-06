"""v0.7.6 B14 — crypto_5min report-only observer tests."""

from __future__ import annotations

from app.services.crypto_5min_observer import Crypto5minObserver, is_crypto_5min


def test_detection_via_slug():
    assert is_crypto_5min(category=None, slug="btc-up-or-down-5min-2026", question=None) is True
    assert is_crypto_5min(category=None, slug="btc-up-or-down-5-minute-2026", question=None) is True


def test_detection_via_category_and_duration():
    assert is_crypto_5min(category="Crypto", slug=None, question=None, duration_minutes=5) is True
    assert is_crypto_5min(category="Crypto", slug=None, question=None, duration_minutes=10) is True
    # Crypto but >10 min: not 5min
    assert is_crypto_5min(category="Crypto", slug=None, question=None, duration_minutes=60) is False


def test_detection_via_question_pattern():
    assert is_crypto_5min(
        category=None, slug="poll-x",
        question="Will BTC be up or down in the next 5min?",
    ) is True
    assert is_crypto_5min(
        category=None, slug=None,
        question="ETH next 5 minute close above 3500?",
    ) is True


def test_no_detection_when_unrelated():
    assert is_crypto_5min(category="Politics", slug="election-2026", question="?") is False
    assert is_crypto_5min(category=None, slug=None, question=None) is False


def test_observer_records_only_matches():
    """Calls with is_match=False must NOT increment any counter."""
    obs = Crypto5minObserver()
    obs.record(is_match=False, latency_ms=200, spread=0.01)
    snap = obs.snapshot()
    assert snap["crypto_5min_detected_count"] == 0
    assert snap["n_obs_latency"] == 0


def test_observer_aggregates_metrics():
    obs = Crypto5minObserver()
    for i in range(10):
        obs.record(
            is_match=True,
            latency_ms=100 + i * 10,
            spread=0.01 + i * 0.001,
            fee_amount=0.05,
            ev_after_fee_value=0.02,
            copy_delay_seconds=15.0,
            price_deterioration=0.005,
        )
    obs.record(is_match=True, rejected_reason="LATE_ENTRY:300s")  # tagged late entry
    snap = obs.snapshot()
    assert snap["crypto_5min_detected_count"] == 11
    assert snap["rejected_by_late_entry"] == 1
    assert snap["latency_p50_ms"] is not None
    assert snap["latency_p95_ms"] is not None
    assert snap["spread_avg"] is not None
    # All values non-None and finite
    assert 0.01 < snap["spread_avg"] < 0.02
    assert abs(snap["fee_avg_usdc"] - 0.05) < 1e-9
    assert snap["report_only_mode"] is True
    assert snap["active_strategy"] is False


def test_observer_empty_snapshot():
    """Empty observer returns Nones, not crashes."""
    snap = Crypto5minObserver().snapshot()
    assert snap["crypto_5min_detected_count"] == 0
    assert snap["latency_p50_ms"] is None
    assert snap["spread_avg"] is None
    assert snap["report_only_mode"] is True
    assert snap["active_strategy"] is False


def test_observer_never_modifies_pipeline_behavior():
    """The observer is a probe — its record() must NEVER raise (it's
    called inline in the polling loop). Even with weird inputs."""
    obs = Crypto5minObserver()
    # negative metrics, None reason, etc.
    obs.record(is_match=True, rejected_reason=None, spread=-1.0, fee_amount=None,
               ev_after_fee_value=None, copy_delay_seconds=None,
               price_deterioration=None, latency_ms=None)
    snap = obs.snapshot()
    # Non-zero detected count from the call
    assert snap["crypto_5min_detected_count"] == 1
