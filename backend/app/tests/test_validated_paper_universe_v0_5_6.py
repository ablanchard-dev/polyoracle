"""Tests for v0.5.6 ValidatedPaperUniverseV056 reconstruction."""

from __future__ import annotations

import csv
from pathlib import Path

from app.services.composite_wallet_score import (
    apply_tier_rules,
    compute as compute_composite,
)
from app.services.edge_quality_engine import WalletEdgeMetrics
from app.services.validated_paper_universe_v0_5_6 import (
    ValidatedPaperUniverseV056,
    WalletInputs,
)


def _make_metrics(
    address: str,
    *,
    sample: int = 120,
    win_rate: float = 0.70,
    ev: float = 0.10,
    ev_lb: float | None = 0.05,
    persistence: float = 1.0,
    holdout_pass: bool = True,
    exclusion_status: list[str] | None = None,
    average_entry_price: float = 0.50,
    activity_recency_score: float = 1.0,
) -> WalletEdgeMetrics:
    return WalletEdgeMetrics(
        address=address,
        sample_size=sample,
        n_winners=int(sample * win_rate),
        n_losers=sample - int(sample * win_rate),
        win_rate=win_rate,
        average_entry_price=average_entry_price,
        average_win_payout=1.0,
        average_loss_cost=-1.0,
        expected_value=ev,
        ev_lower_bound=ev_lb,
        ev_confidence="HIGH" if sample >= 100 else "MEDIUM",
        risk_reward_ratio=1.0,
        breakeven_win_rate=0.50,
        price_entry_sanity="OK",
        persistence_score=persistence,
        persistence_details={"usable_quartiles": 4, "positive_quartiles": int(persistence * 4), "per_quartile": []},
        holdout_pass=holdout_pass,
        holdout_details={"verdict": "PASS" if holdout_pass else "OVERFIT_RISK"},
        category_edge_map={"Politics": {"sample": sample, "win_rate": win_rate, "ev": ev}},
        regime_robustness={"usable_eras": 2, "positive_eras": 2, "fraction_positive": 1.0, "per_era": []},
        activity_recency_weeks=1.5,
        activity_recency_score=activity_recency_score,
        position_size_sanity={"is_sane": True, "reason": "ok"},
        temporal_profile={},
        avg_position_notional_usd=500.0,
        total_notional_usd=60_000.0,
        weeks_observed=50.0,
        estimated_trades_per_week=2.4,
        expected_weekly_R=ev * 2.4,
        exclusion_status=list(exclusion_status or []),
        notes=[],
    )


def _make_inputs(
    metrics: WalletEdgeMetrics,
    *,
    prior_tier: str | None = None,
    val_wr: float | None = 0.80,
    val_gap: float | None = 0.05,
    lost_gem_action: str | None = None,
) -> WalletInputs:
    bk = compute_composite(metrics, validation_win_rate=val_wr, validation_gap=val_gap)
    dec = apply_tier_rules(
        metrics,
        bk,
        validation_win_rate=val_wr,
        validation_gap=val_gap,
        elite_min_composite=50.0,
        elite_min_ev_lower_bound=0.01,
        strong_min_composite=40.0,
        strong_min_ev_lower_bound=0.0,
    )
    return WalletInputs(
        address=metrics.address,
        metrics=metrics,
        breakdown=bk,
        decision=dec,
        prior_tier=prior_tier,
        validation_win_rate=val_wr,
        validation_gap=val_gap,
        lost_gem_action=lost_gem_action,
    )


def test_universe_excludes_negative_ev_wallets(tmp_path: Path) -> None:
    builder = ValidatedPaperUniverseV056(tmp_path)
    bad = _make_metrics(
        "0xneg",
        ev=-0.05,
        ev_lb=-0.10,
        average_entry_price=0.95,
        exclusion_status=["NEGATIVE_EV", "EV_LOWER_BOUND_NEGATIVE", "BAD_ENTRY_PRICE_PATTERN"],
    )
    good = _make_metrics("0xgood")
    summary = builder.build(
        [_make_inputs(bad), _make_inputs(good)],
        elite_threshold_composite=50.0,
        strong_threshold_composite=40.0,
        elite_threshold_ev_lower_bound=0.01,
        strong_threshold_ev_lower_bound=0.0,
    )
    assert summary.n_elite >= 1
    # The bad wallet is never ELITE/STRONG.
    csv_path = Path(summary.csv_path)
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    bad_row = next(r for r in rows if r["address"] == "0xneg")
    assert bad_row["tier_v0_5_6"] not in {"ELITE_EV_VALIDATED", "STRONG_EV_VALIDATED"}
    assert bad_row["allowed_safe"] == "False"
    assert bad_row["allowed_aggressive"] == "False"


def test_false_elite_export_lists_demoted_priors(tmp_path: Path) -> None:
    builder = ValidatedPaperUniverseV056(tmp_path)
    # Was ELITE in v0.5.5, but EV is negative now — must show up in false_elite.
    demoted = _make_metrics(
        "0xdemoted",
        ev=-0.10,
        ev_lb=-0.20,
        average_entry_price=0.90,
        exclusion_status=["NEGATIVE_EV", "EV_LOWER_BOUND_NEGATIVE", "BAD_ENTRY_PRICE_PATTERN"],
    )
    survivor = _make_metrics("0xsurvivor")
    builder.build(
        [
            _make_inputs(demoted, prior_tier="ELITE"),
            _make_inputs(survivor, prior_tier="ELITE"),
        ],
        elite_threshold_composite=50.0,
        strong_threshold_composite=40.0,
        elite_threshold_ev_lower_bound=0.01,
        strong_threshold_ev_lower_bound=0.0,
    )
    false_path = tmp_path / "false_elite_removed.csv"
    rows = list(csv.DictReader(false_path.open(encoding="utf-8")))
    assert any(r["address"] == "0xdemoted" for r in rows)
    assert all(r["address"] != "0xsurvivor" for r in rows)


def test_lost_gems_export_lists_reintegrated_wallets(tmp_path: Path) -> None:
    builder = ValidatedPaperUniverseV056(tmp_path)
    # Wallet had no prior tier, comes through clean → lost gem reintegrated.
    gem = _make_metrics("0xgem")
    in_universe = _make_metrics("0xpre", win_rate=0.65)
    builder.build(
        [
            _make_inputs(gem, prior_tier=None, lost_gem_action="REINTEGRATE"),
            _make_inputs(in_universe, prior_tier="STRONG"),
        ],
        elite_threshold_composite=50.0,
        strong_threshold_composite=40.0,
        elite_threshold_ev_lower_bound=0.01,
        strong_threshold_ev_lower_bound=0.0,
    )
    gems_path = tmp_path / "lost_gems_reintegrated.csv"
    rows = list(csv.DictReader(gems_path.open(encoding="utf-8")))
    assert any(r["address"] == "0xgem" for r in rows)


def test_allowed_safe_requires_high_ev_lower_bound_and_persistence(tmp_path: Path) -> None:
    builder = ValidatedPaperUniverseV056(
        tmp_path,
        elite_safe_min_ev_lower_bound=0.05,
        elite_safe_min_persistence=0.75,
    )
    # ELITE-passing but EV_lb only 0.02 (below safe gate of 0.05).
    elite_low_lb = _make_metrics("0xlow_lb", ev_lb=0.02)
    elite_high_lb = _make_metrics("0xhigh_lb", ev_lb=0.10, persistence=1.0)
    summary = builder.build(
        [_make_inputs(elite_low_lb), _make_inputs(elite_high_lb)],
        elite_threshold_composite=40.0,
        strong_threshold_composite=30.0,
        elite_threshold_ev_lower_bound=0.01,
        strong_threshold_ev_lower_bound=0.0,
    )
    csv_path = Path(summary.csv_path)
    rows = {r["address"]: r for r in csv.DictReader(csv_path.open(encoding="utf-8"))}
    # Both could be ELITE_EV_VALIDATED at composite=40 threshold but only the
    # high-lb one earns allowed_safe.
    assert rows["0xhigh_lb"]["tier_v0_5_6"] == "ELITE_EV_VALIDATED"
    assert rows["0xhigh_lb"]["allowed_safe"] == "True"
    assert rows["0xlow_lb"]["allowed_safe"] == "False"


def test_blocking_status_collapses_to_ignore(tmp_path: Path) -> None:
    builder = ValidatedPaperUniverseV056(tmp_path)
    blocked = _make_metrics(
        "0xblocked",
        ev=0.10,
        ev_lb=0.05,
        exclusion_status=["NEGATIVE_EV"],  # paradox but enforced anyway
    )
    builder.build(
        [_make_inputs(blocked)],
        elite_threshold_composite=40.0,
        strong_threshold_composite=30.0,
        elite_threshold_ev_lower_bound=0.01,
        strong_threshold_ev_lower_bound=0.0,
    )
    csv_path = tmp_path / "validated_paper_universe_v0_5_6.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    blocked_row = next(r for r in rows if r["address"] == "0xblocked")
    assert blocked_row["tier_v0_5_6"] not in {"ELITE_EV_VALIDATED", "STRONG_EV_VALIDATED"}
