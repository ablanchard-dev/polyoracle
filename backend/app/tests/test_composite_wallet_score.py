"""Tests for v0.5.6 Phase A — CompositeWalletScore + tier rules."""

from __future__ import annotations

import pytest

from app.services.composite_wallet_score import (
    apply_tier_rules,
    compute as compute_composite,
)
from app.services.edge_quality_engine import WalletEdgeMetrics


def _metrics(**overrides: object) -> WalletEdgeMetrics:
    base = WalletEdgeMetrics(
        address=str(overrides.pop("address", "0xfake")),
        sample_size=int(overrides.pop("sample_size", 120)),
        n_winners=int(overrides.pop("n_winners", 80)),
        n_losers=int(overrides.pop("n_losers", 40)),
        win_rate=float(overrides.pop("win_rate", 0.6667)),
        average_entry_price=float(overrides.pop("average_entry_price", 0.50)),
        average_win_payout=float(overrides.pop("average_win_payout", 1.0)),
        average_loss_cost=float(overrides.pop("average_loss_cost", -1.0)),
        expected_value=float(overrides.pop("expected_value", 0.10)),
        ev_lower_bound=overrides.pop("ev_lower_bound", 0.05),
        ev_confidence=str(overrides.pop("ev_confidence", "HIGH")),
        risk_reward_ratio=float(overrides.pop("risk_reward_ratio", 1.0)),
        breakeven_win_rate=float(overrides.pop("breakeven_win_rate", 0.50)),
        price_entry_sanity=str(overrides.pop("price_entry_sanity", "OK")),
        persistence_score=float(overrides.pop("persistence_score", 1.0)),
        persistence_details=overrides.pop("persistence_details", {"usable_quartiles": 4, "positive_quartiles": 4, "per_quartile": []}),  # type: ignore[arg-type]
        holdout_pass=bool(overrides.pop("holdout_pass", True)),
        holdout_details=overrides.pop("holdout_details", {"verdict": "PASS"}),  # type: ignore[arg-type]
        category_edge_map=overrides.pop("category_edge_map", {"Politics": {"sample": 80, "win_rate": 0.7, "ev": 0.10}}),  # type: ignore[arg-type]
        regime_robustness=overrides.pop("regime_robustness", {"usable_eras": 2, "positive_eras": 2, "fraction_positive": 1.0, "per_era": []}),  # type: ignore[arg-type]
        activity_recency_weeks=float(overrides.pop("activity_recency_weeks", 1.5)),
        activity_recency_score=float(overrides.pop("activity_recency_score", 1.0)),
        position_size_sanity=overrides.pop("position_size_sanity", {"is_sane": True, "reason": "ok", "avg_notional": 500.0}),  # type: ignore[arg-type]
        temporal_profile=overrides.pop("temporal_profile", {}),  # type: ignore[arg-type]
        avg_position_notional_usd=float(overrides.pop("avg_position_notional_usd", 500.0)),
        total_notional_usd=float(overrides.pop("total_notional_usd", 60_000.0)),
        weeks_observed=float(overrides.pop("weeks_observed", 50.0)),
        estimated_trades_per_week=float(overrides.pop("estimated_trades_per_week", 2.4)),
        expected_weekly_R=float(overrides.pop("expected_weekly_R", 0.24)),
        exclusion_status=list(overrides.pop("exclusion_status", [])),  # type: ignore[arg-type]
        notes=list(overrides.pop("notes", [])),  # type: ignore[arg-type]
    )
    if overrides:
        raise AssertionError(f"unused overrides: {overrides}")
    return base


def test_composite_score_within_0_100() -> None:
    bk = compute_composite(_metrics(), validation_win_rate=0.80, validation_gap=0.05)
    assert 0.0 <= bk.composite <= 100.0
    for k, v in bk.sub_scores.items():
        assert 0.0 <= v <= 100.0, f"{k}={v}"


def test_subscore_weights_sum_to_one() -> None:
    bk = compute_composite(_metrics(), validation_win_rate=0.80, validation_gap=0.05)
    assert abs(sum(bk.weights.values()) - 1.0) < 1e-9


def test_ev_lower_bound_drives_score_monotonically() -> None:
    """All else equal, higher EV_lower_bound → higher composite."""
    low = compute_composite(_metrics(ev_lower_bound=-0.10), validation_win_rate=0.80, validation_gap=0.05)
    mid = compute_composite(_metrics(ev_lower_bound=0.0), validation_win_rate=0.80, validation_gap=0.05)
    high = compute_composite(_metrics(ev_lower_bound=0.15), validation_win_rate=0.80, validation_gap=0.05)
    assert low.composite < mid.composite < high.composite


def test_negative_ev_lower_bound_blocks_elite() -> None:
    m = _metrics(ev_lower_bound=-0.05, exclusion_status=["EV_LOWER_BOUND_NEGATIVE"])
    bk = compute_composite(m, validation_win_rate=0.85, validation_gap=0.05)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.85, validation_gap=0.05)
    assert dec.tier != "ELITE"


def test_holdout_failed_blocks_elite() -> None:
    m = _metrics(
        holdout_pass=False,
        holdout_details={"verdict": "OVERFIT_RISK"},
        exclusion_status=["OVERFIT_RISK"],
    )
    bk = compute_composite(m, validation_win_rate=0.85, validation_gap=0.05)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.85, validation_gap=0.05)
    assert dec.tier != "ELITE"


def test_persistence_below_threshold_blocks_elite() -> None:
    m = _metrics(persistence_score=0.50)
    bk = compute_composite(m, validation_win_rate=0.85, validation_gap=0.05)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.85, validation_gap=0.05)
    assert dec.tier != "ELITE"


def test_clean_metrics_become_elite_with_lenient_thresholds() -> None:
    m = _metrics()  # all clean
    bk = compute_composite(m, validation_win_rate=0.80, validation_gap=0.05)
    dec = apply_tier_rules(
        m,
        bk,
        validation_win_rate=0.80,
        validation_gap=0.05,
        elite_min_composite=50.0,
        elite_min_ev_lower_bound=0.01,
    )
    assert dec.tier == "ELITE"


def test_bad_entry_price_pattern_lands_suspicious() -> None:
    m = _metrics(
        average_entry_price=0.95,
        expected_value=-0.04,
        win_rate=0.92,
        ev_lower_bound=-0.05,
        exclusion_status=["NEGATIVE_EV", "EV_LOWER_BOUND_NEGATIVE", "BAD_ENTRY_PRICE_PATTERN"],
    )
    bk = compute_composite(m, validation_win_rate=0.92, validation_gap=0.03)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.92, validation_gap=0.03)
    # Either SUSPICIOUS or IGNORE — the key invariant is *not* ELITE/STRONG/CANDIDATE.
    assert dec.tier in {"SUSPICIOUS", "IGNORE"}


def test_validation_gap_above_15pct_blocks_elite() -> None:
    m = _metrics()
    bk = compute_composite(m, validation_win_rate=0.80, validation_gap=0.20)
    dec = apply_tier_rules(
        m,
        bk,
        validation_win_rate=0.80,
        validation_gap=0.20,
        elite_min_composite=50.0,
        elite_min_ev_lower_bound=0.01,
    )
    assert dec.tier != "ELITE"


def test_low_validation_winrate_blocks_elite() -> None:
    m = _metrics()
    bk = compute_composite(m, validation_win_rate=0.55, validation_gap=0.05)
    dec = apply_tier_rules(
        m,
        bk,
        validation_win_rate=0.55,
        validation_gap=0.05,
        elite_min_composite=50.0,
        elite_min_ev_lower_bound=0.01,
    )
    assert dec.tier != "ELITE"


def test_sample_below_30_blocks_strong() -> None:
    m = _metrics(sample_size=20, n_winners=15, n_losers=5)
    bk = compute_composite(m, validation_win_rate=0.75, validation_gap=0.05)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.75, validation_gap=0.05)
    assert dec.tier not in {"ELITE", "STRONG"}


def test_unknown_validation_does_not_grant_elite() -> None:
    """A wallet with no OOS data cannot be ELITE even if metrics look great."""
    m = _metrics()
    bk = compute_composite(m, validation_win_rate=None, validation_gap=None)
    dec = apply_tier_rules(
        m,
        bk,
        validation_win_rate=None,
        validation_gap=None,
        elite_min_composite=50.0,
        elite_min_ev_lower_bound=0.01,
    )
    assert dec.tier != "ELITE"
