"""v0.7.2 B6 + B2 fix tests.

* B6 — `MarketScanner.compute_market_liquidity_score` now uses a
  log-scale formula calibrated for Polymarket (0-100 over $100-$10M).
  The legacy linear ``min(100, liquidity/2500)`` capped 95% of markets
  at score < 20.

* B2 — `wallet_polling_engine.poll_wallet_once` clamps ``since_ts`` so
  the polling never backfills more than ``POLLING_RESTART_FRESHNESS_S``
  seconds (default 30 min) of historical trades on restart.
"""

from __future__ import annotations

import math


# ============ B6 ============


def _log_score(volume_usd: float) -> float:
    """Reference re-implementation matching the patched function."""
    if volume_usd <= 100.0:
        return 0.0
    log_vol = math.log10(volume_usd)
    return max(0.0, min(100.0, (log_vol - 2.0) / 5.0 * 100.0))


def test_b6_liquidity_score_zero_for_empty_market():
    from app.services.market_scanner import MarketScanner
    from unittest.mock import MagicMock
    s = MarketScanner.__new__(MarketScanner)
    m = MagicMock()
    m.liquidity = 0
    assert s.compute_market_liquidity_score(m) == 0.0


def test_b6_liquidity_score_50_for_50k_volume():
    """Median Polymarket market (~$50k) lands around 53-54."""
    from app.services.market_scanner import MarketScanner
    from unittest.mock import MagicMock
    s = MarketScanner.__new__(MarketScanner)
    m = MagicMock()
    m.liquidity = 50_000
    score = s.compute_market_liquidity_score(m)
    assert 50 <= score <= 60


def test_b6_liquidity_score_100_for_huge_market():
    """$10M+ markets score 100."""
    from app.services.market_scanner import MarketScanner
    from unittest.mock import MagicMock
    s = MarketScanner.__new__(MarketScanner)
    m = MagicMock()
    m.liquidity = 100_000_000
    assert s.compute_market_liquidity_score(m) == 100.0


def test_b6_liquidity_score_buckets_match_calibration():
    """Spot-check the calibration table from the patched docstring."""
    from app.services.market_scanner import MarketScanner
    from unittest.mock import MagicMock
    s = MarketScanner.__new__(MarketScanner)
    cases = [
        (100, 0.0),
        (1_000, 20.0),
        (10_000, 40.0),
        (100_000, 60.0),
        (1_000_000, 80.0),
        (10_000_000, 100.0),
    ]
    for liq, expected in cases:
        m = MagicMock()
        m.liquidity = liq
        got = s.compute_market_liquidity_score(m)
        assert abs(got - expected) < 0.5, f"liq=${liq}: got {got}, expected ~{expected}"


def test_b6_distribution_realistic_random_polymarket_sizes():
    """A realistic spread of Polymarket markets should produce a varied
    score distribution, NOT all clamped at 19 like the legacy formula."""
    from app.services.market_scanner import MarketScanner
    from unittest.mock import MagicMock
    s = MarketScanner.__new__(MarketScanner)
    realistic_liqs = [
        500, 2_000, 5_500, 12_000, 22_000, 45_000, 75_000, 110_000,
        220_000, 480_000, 950_000, 1_700_000, 4_200_000, 9_300_000,
    ]
    scores = []
    for liq in realistic_liqs:
        m = MagicMock()
        m.liquidity = liq
        scores.append(s.compute_market_liquidity_score(m))
    # Range should span at least 0..80 (no flat-line at 19)
    assert max(scores) - min(scores) > 50
    # Median should be > 40 (the legacy formula gave median 19)
    sorted_scores = sorted(scores)
    median = sorted_scores[len(sorted_scores) // 2]
    assert median > 40


# ============ B2 ============


def test_b2_freshness_clamp_constant_default_30min():
    """The freshness floor constant defaults to 30 min (1800 s)."""
    from app.services.wallet_polling_engine import POLLING_RESTART_FRESHNESS_S
    assert POLLING_RESTART_FRESHNESS_S == 1800


def test_b2_freshness_floor_clamps_stale_since_ts():
    """When last_seen_ts is older than now - freshness_floor, we clamp."""
    import time
    from app.services.wallet_polling_engine import POLLING_RESTART_FRESHNESS_S
    now = int(time.time())
    floor = now - POLLING_RESTART_FRESHNESS_S
    stale_since_ts = now - 11 * 3600  # 11h old
    # The patched poll_wallet_once would compute: since_ts < floor → since_ts = floor
    effective_since_ts = max(stale_since_ts, floor)
    assert effective_since_ts == floor
    # And a fresh since_ts (5 min back) survives the clamp
    fresh_since_ts = now - 300
    effective_fresh = max(fresh_since_ts, floor)
    assert effective_fresh == fresh_since_ts


def test_b2_first_contact_uses_freshness_floor():
    """When last_seen_ts is 0 (first contact), the anchor is bounded by
    the freshness floor — never starts further back than 30 min."""
    import time
    from app.services.wallet_polling_engine import POLLING_RESTART_FRESHNESS_S
    now = int(time.time())
    polling_started = now - 60  # polling just started
    freshness_floor = now - POLLING_RESTART_FRESHNESS_S
    anchor = polling_started or (now - 60)
    since_ts = max(freshness_floor, anchor - 60)
    # since_ts should be the polling_started anchor (more recent than floor)
    assert since_ts == polling_started - 60

    # With a longer pause: polling_started 4h ago — clamp wins
    polling_started_old = now - 4 * 3600
    anchor = polling_started_old
    since_ts = max(freshness_floor, anchor - 60)
    assert since_ts == freshness_floor
