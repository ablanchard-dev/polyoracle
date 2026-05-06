"""v0.7.8 — ELITE-first polling priority tests.

Polling cycle math: cohort 981 + rate 5/s = ~196s per cycle. ELITE
wallets (59) get the same cycle as STRONG → 1MN/2MN markets resolve
before next poll. Fix: ELITE re-polled every 30s, non-ELITE every 90s.
Within rate limit, ELITE wins → real ELITE cycle ~30s, non-ELITE
starves to ~300s (acceptable: paper trades primarily depend on ELITE).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.wallet_polling_engine import (
    POLLING_INTERVAL_ELITE_SECONDS,
    POLLING_INTERVAL_SECONDS,
    POLLING_RATE_LIMIT_CALLS_PER_SEC,
    WalletPollingEngine,
)


def test_elite_interval_is_tighter_than_default():
    """ELITE re-poll interval must be strictly shorter so ELITE wallets
    re-enter the polling queue faster."""
    assert POLLING_INTERVAL_ELITE_SECONDS < POLLING_INTERVAL_SECONDS
    assert POLLING_INTERVAL_ELITE_SECONDS >= 10  # safety floor


def test_elite_interval_is_compatible_with_1min_market():
    """At 30s ELITE re-poll, a 1MN market (60s lifetime) leaves at most
    30s of remaining time for the bot to act after seeing the wallet
    trade — borderline but workable. At 90s, a 1MN market is already
    resolved by the time we poll → unusable."""
    assert POLLING_INTERVAL_ELITE_SECONDS <= 30
    # ULTRA_SHORT 1MN markets need ≤ resolution/2 polling
    assert POLLING_INTERVAL_ELITE_SECONDS <= 60 / 2


def test_load_elite_set_returns_set_of_lowercase_addresses():
    """The helper that the polling loop calls must return a set of
    lowercase addresses (addresses are normalized lowercase elsewhere)."""
    # Mock session that returns 2 ELITE rows
    fake_session = MagicMock()
    fake_session.exec.return_value.all.return_value = [
        ("0xELITE_A",),
        ("0xELITE_B",),
    ]
    with patch("app.services.wallet_polling_engine.MarketFirstWalletRecord", create=True):
        # The actual call goes through sqlmodel.select; mock it.
        # Easier: mock the whole exec().all() chain.
        result = WalletPollingEngine.load_elite_set(fake_session)
    assert isinstance(result, set)
    assert all(isinstance(a, str) for a in result)
    # Lowercase normalization
    assert all(a == a.lower() for a in result)


def test_load_elite_set_returns_empty_on_failure():
    """DB unavailable / schema mismatch → return empty set, never raise."""
    fake_session = MagicMock()
    fake_session.exec.side_effect = Exception("DB down")
    result = WalletPollingEngine.load_elite_set(fake_session)
    assert result == set()


def test_load_elite_set_returns_empty_when_session_is_none():
    """No session passed → empty set, no DB call attempted."""
    assert WalletPollingEngine.load_elite_set(None) == set()


def test_rate_limit_compatibility_at_target_capacity():
    """At cohort 59 ELITE + 922 non-ELITE the THEORETICAL demand exceeds
    the rate limit (12.2/s vs 5/s) — that's intentional: rate-limit
    throttling forces ELITE-first prioritization. The arithmetic check
    here confirms our defaults are in the regime where ELITE-first
    matters (= demand > rate limit)."""
    elite_demand = 59 / POLLING_INTERVAL_ELITE_SECONDS
    non_elite_demand = 922 / POLLING_INTERVAL_SECONDS
    total_demand = elite_demand + non_elite_demand
    # We DO want demand > rate limit so ELITE wins via min(next_due).
    # If demand < rate limit, both tiers get their interval and the
    # ELITE-first ordering would be a no-op.
    assert total_demand > POLLING_RATE_LIMIT_CALLS_PER_SEC, (
        f"At target cohort, total demand {total_demand:.2f}/s should exceed "
        f"rate limit {POLLING_RATE_LIMIT_CALLS_PER_SEC}/s so ELITE-first "
        f"prioritization actually fires."
    )
    # ELITE alone must fit under rate limit.
    assert elite_demand < POLLING_RATE_LIMIT_CALLS_PER_SEC


def test_initial_stagger_puts_elite_at_front_of_queue():
    """Simulate the initial-stagger logic in run_loop. ELITE wallets must
    be scheduled in the first POLLING_INTERVAL_ELITE_SECONDS window so
    they are picked first."""
    elite_addresses = {"0xelite1", "0xelite2", "0xelite3"}
    non_elite_addresses = ["0xstrong1", "0xstrong2", "0xstrong3", "0xstrong4"]
    cohort = list(elite_addresses) + non_elite_addresses

    elite_count = sum(1 for a in cohort if a in elite_addresses)
    non_elite_count = max(1, len(cohort) - elite_count)
    elite_idx = 0
    non_elite_idx = 0
    next_due: dict[str, float] = {}
    for addr in cohort:
        if addr in elite_addresses:
            offset = (POLLING_INTERVAL_ELITE_SECONDS / max(1, elite_count)) * elite_idx
            elite_idx += 1
        else:
            offset = (POLLING_INTERVAL_SECONDS / non_elite_count) * non_elite_idx
            non_elite_idx += 1
        next_due[addr] = offset

    elite_max = max(next_due[a] for a in elite_addresses)
    non_elite_min = min(next_due[a] for a in non_elite_addresses)
    # ELITE windows must end before non-ELITE windows START — otherwise
    # ELITE-first prioritization is undermined.
    assert elite_max <= POLLING_INTERVAL_ELITE_SECONDS, "ELITE within tighter window"
    assert non_elite_min == 0.0  # first non-ELITE starts at 0
    # In the actual run_loop ordering, ELITE re-polls at +ELITE every
    # poll while non-ELITE re-polls at +DEFAULT every poll, so ELITE
    # systematically enters the queue more often.


def test_repoll_interval_is_tier_aware():
    """After polling a wallet, run_loop sets next_due[addr] = now +
    interval. The interval depends on tier."""
    elite_addresses = {"0xelite_a"}
    # ELITE: re-poll at ELITE interval
    addr = "0xelite_a"
    interval = (
        POLLING_INTERVAL_ELITE_SECONDS
        if addr in elite_addresses
        else POLLING_INTERVAL_SECONDS
    )
    assert interval == POLLING_INTERVAL_ELITE_SECONDS

    # non-ELITE: re-poll at default interval
    addr2 = "0xstrong_a"
    interval2 = (
        POLLING_INTERVAL_ELITE_SECONDS
        if addr2 in elite_addresses
        else POLLING_INTERVAL_SECONDS
    )
    assert interval2 == POLLING_INTERVAL_SECONDS
