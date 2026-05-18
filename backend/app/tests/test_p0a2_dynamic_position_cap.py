"""P0-A2 (2026-05-18) — tier-dynamic position cap overrides profile cap.

Bug found post-P0-A: bot ran with AGGRESSIVE risk profile which hardcodes
max_open_positions=15. With effective_capital ~$76k (tier HUGE), the tier
allows 960 positions but the profile cap blocked the bot at 15. Fix: caller
passes tier_max_positions; if set, it governs; profile cap stays only as
fallback for legacy/non-tier-aware callers.
"""

from app.services.risk_engine import RiskEngine
from app.services.risk_mode import AGGRESSIVE, FULL_PAPER, SAFE


COMMON = dict(
    candidate_status="ELITE",
    market_sample_size=200,
    win_rate_confidence="HIGH",
    signal_score=80,
    spread=0.01,
    liquidity=10_000,
    copyable_edge_score=100.0,
    orderbook_quality="GOOD",
)


def test_tier_cap_overrides_aggressive_profile_cap_15() -> None:
    """HUGE tier (960) must override AGGRESSIVE.max_open_positions=15.
    Bot with 15 open positions and effective_capital=$76k should NOT be rejected.
    """
    decision = RiskEngine().validate_for_mode(
        AGGRESSIVE,
        open_positions_count=15,
        tier_max_positions=960,
        **COMMON,
    )
    assert decision.approved is True, f"Expected approval, got: {decision.reason}"


def test_tier_cap_rejects_when_open_eq_tier() -> None:
    """At tier cap, reject. open=960, tier=960 → TOO_MUCH_EXPOSURE."""
    decision = RiskEngine().validate_for_mode(
        AGGRESSIVE,
        open_positions_count=960,
        tier_max_positions=960,
        **COMMON,
    )
    assert decision.approved is False
    assert decision.reason_code == "TOO_MUCH_EXPOSURE"
    assert decision.details["cap_source"] == "tier"
    assert decision.details["final_cap_used"] == 960
    assert decision.details["profile_cap"] == 15
    assert decision.details["tier_cap"] == 960


def test_tier_cap_rejects_when_open_above_tier() -> None:
    """open > tier_cap → reject regardless of profile."""
    decision = RiskEngine().validate_for_mode(
        FULL_PAPER,  # no profile cap (max_open_positions=None)
        open_positions_count=1000,
        tier_max_positions=960,
        **COMMON,
    )
    assert decision.approved is False
    assert decision.reason_code == "TOO_MUCH_EXPOSURE"
    assert decision.details["cap_source"] == "tier"
    assert decision.details["final_cap_used"] == 960


def test_profile_cap_fallback_when_no_tier_cap() -> None:
    """Backward compat: no tier_max_positions → profile cap applies."""
    decision = RiskEngine().validate_for_mode(
        AGGRESSIVE,
        open_positions_count=15,
        # tier_max_positions=None (default) → fallback to profile.max_open_positions=15
        **COMMON,
    )
    assert decision.approved is False
    assert decision.reason_code == "TOO_MUCH_EXPOSURE"
    assert decision.details["cap_source"] == "profile"
    assert decision.details["final_cap_used"] == 15
    assert decision.details["tier_cap"] is None


def test_no_cap_when_both_unset() -> None:
    """FULL_PAPER (profile cap=None) + no tier_max_positions → no positions cap.
    Other gates should still apply but cap check is skipped."""
    decision = RiskEngine().validate_for_mode(
        FULL_PAPER,
        open_positions_count=5000,  # huge count, no cap should apply
        **COMMON,
    )
    assert decision.approved is True, f"Expected approval, got: {decision.reason}"


def test_nano_tier_cap_40() -> None:
    """NANO tier (40) — bot at 40 positions rejects, at 39 accepts."""
    base = RiskEngine().validate_for_mode(
        AGGRESSIVE, open_positions_count=39, tier_max_positions=40, **COMMON
    )
    assert base.approved is True

    blocked = RiskEngine().validate_for_mode(
        AGGRESSIVE, open_positions_count=40, tier_max_positions=40, **COMMON
    )
    assert blocked.approved is False
    assert blocked.details["final_cap_used"] == 40
    assert blocked.details["cap_source"] == "tier"


def test_safe_profile_keeps_strict_status_gates() -> None:
    """SAFE profile rejects WATCH (not in allowed_statuses) even with generous tier cap.
    Confirms P0-A2 doesn't loosen other gates."""
    args = {**COMMON, "candidate_status": "WATCH"}  # SAFE allows {STRONG, ELITE}
    decision = RiskEngine().validate_for_mode(
        SAFE,
        open_positions_count=0,
        tier_max_positions=960,
        **args,
    )
    assert decision.approved is False
    assert decision.reason_code == "WALLET_NOT_RELIABLE"
