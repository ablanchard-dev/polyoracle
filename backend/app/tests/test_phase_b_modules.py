"""v0.5.6 Phase B unit tests — calibration / category gate / Kelly /
decay / copyable / adversarial.
"""

from __future__ import annotations

from app.services.adversarial_filter import (
    copy_allowed_if_market_matches_edge,
    market_similarity_score,
)
from app.services.category_matching_gate import evaluate as evaluate_category
from app.services.copyable_edge_dynamic import evaluate as evaluate_copy
from app.services.kelly_cap import KELLY_HARD_CAP, recommend
from app.services.threshold_calibration import (
    CalibrationCandidate,
    build_candidates,
    evaluate_candidate,
)
from app.services.wallet_decay import (
    DECAY_PER_WINDOW_POINTS,
    apply_decay,
    revoke_allowed_safe,
)
from app.tests.test_validated_paper_universe_v0_5_6 import _make_metrics


# ---------------- threshold calibration ----------------


def test_build_candidates_uses_positive_ev_lb_percentiles() -> None:
    metrics = [
        _make_metrics(f"0x{k:040x}", ev_lb=0.01 + k * 0.005)
        for k in range(20)
    ]
    cands = build_candidates(metrics)
    assert len(cands) == 4 * 4 * 3 * 3  # 4 ev_lb × 4 comp × 3 pers × 3 gap
    assert all(isinstance(c, CalibrationCandidate) for c in cands)
    # All thresholds are >0 (positive subset only).
    assert all(c.elite_min_ev_lower_bound > 0 for c in cands)


def test_evaluate_candidate_returns_zero_elite_when_threshold_too_high() -> None:
    m = _make_metrics("0xa", ev_lb=0.05)
    breakdowns = {"0xa": _b(m)}
    cand = CalibrationCandidate(
        elite_min_ev_lower_bound=0.50,  # nobody clears this
        elite_min_composite=85.0,
        elite_min_persistence=0.95,
        max_validation_gap=0.05,
    )
    res = evaluate_candidate(cand, {"0xa": m}, breakdowns, {"0xa": (0.85, 0.05)})
    assert res.n_elite == 0


# ---------------- category matching ----------------


def test_category_gate_blocks_negative_ev_categories() -> None:
    edge_map = {"Politics": {"sample": 80, "ev": 0.05}, "Sports": {"sample": 30, "ev": -0.02}}
    out_pol = evaluate_category("Politics", edge_map)
    out_spo = evaluate_category("Sports", edge_map)
    assert out_pol.allowed and out_pol.reason == "OK"
    assert not out_spo.allowed and out_spo.reason == "NEGATIVE_CAT_EV"


def test_category_gate_blocks_missing_or_underweight() -> None:
    edge_map = {"Politics": {"sample": 80, "ev": 0.05}}
    assert not evaluate_category("Crypto", edge_map).allowed
    edge_map_thin = {"Crypto": {"sample": 2, "ev": 0.20}}
    out = evaluate_category("Crypto", edge_map_thin)
    assert not out.allowed and out.reason == "INSUFFICIENT_SAMPLE"


# ---------------- Kelly cap ----------------


def test_kelly_capped_at_2pct() -> None:
    m = _make_metrics(
        "0xkelly",
        ev_lb=0.50,  # huge edge
        average_entry_price=0.30,
    )
    rec = recommend(m)
    assert rec.capped_fraction <= KELLY_HARD_CAP
    assert rec.full_kelly_fraction is not None and rec.full_kelly_fraction > KELLY_HARD_CAP


def test_kelly_zero_when_negative_ev_lb() -> None:
    m = _make_metrics("0xneg", ev_lb=-0.05)
    rec = recommend(m)
    assert rec.capped_fraction == 0.0


# ---------------- decay ----------------


def test_no_decay_within_2_weeks() -> None:
    out = apply_decay("0xa", 80.0, 1.0)
    assert out.decayed_composite == 80.0
    assert out.status_suffix == ""


def test_decay_per_window_at_5_points() -> None:
    out = apply_decay("0xa", 80.0, 4.0)  # 2 windows past the 14d grace
    assert out.decayed_composite == 80.0 - DECAY_PER_WINDOW_POINTS * 1
    out_4w = apply_decay("0xa", 80.0, 6.0)
    assert out_4w.decayed_composite == 80.0 - DECAY_PER_WINDOW_POINTS * 2


def test_stale_after_26_weeks() -> None:
    out = apply_decay("0xa", 80.0, 27.0)
    assert out.status_suffix == "_STALE"
    assert revoke_allowed_safe(27.0)


def test_revalidation_after_52_weeks() -> None:
    out = apply_decay("0xa", 80.0, 60.0)
    assert out.status_suffix == "_REVALIDATION_REQUIRED"


# ---------------- copyable edge ----------------


def test_copy_allowed_when_price_close_to_wallet() -> None:
    out = evaluate_copy(0.50, 0.51)
    assert out.copy_allowed
    assert out.edge_retained_ratio > 0.90


def test_copy_blocked_when_price_above_ceiling() -> None:
    out = evaluate_copy(0.50, 0.96)
    assert not out.copy_allowed
    assert out.reason == "COPY_PRICE_ABOVE_HARD_CEILING"


def test_copy_blocked_when_edge_retained_too_low() -> None:
    # wallet edge at 0.30 → (1-0.3)/0.3 = 2.33; copy at 0.70 → 0.43; ratio = 0.18
    out = evaluate_copy(0.30, 0.70)
    assert not out.copy_allowed
    assert out.edge_retained_ratio < 0.50


# ---------------- adversarial filter ----------------


def test_market_similarity_high_when_category_and_slug_match() -> None:
    edge_map = {"Politics": {"sample": 80, "ev": 0.10}}
    history = ["election-2024-trump-vs-harris", "trump-impeachment-2025"]
    v = market_similarity_score("Politics", "trump-2026-rumors", edge_map, history)
    assert v.category_match is True
    assert v.similarity_score >= 0.5


def test_market_similarity_low_when_category_or_slug_mismatch() -> None:
    edge_map = {"Politics": {"sample": 80, "ev": 0.10}}
    history = ["election-2024-trump-vs-harris"]
    v = market_similarity_score("Crypto", "btc-100k-by-eoy", edge_map, history)
    assert v.similarity_score < 0.5
    allowed, _ = copy_allowed_if_market_matches_edge(
        "Crypto", "btc-100k-by-eoy", edge_map, history
    )
    assert not allowed


# ---------------- helpers ----------------


def _b(m):  # type: ignore[no-untyped-def]
    from app.services.composite_wallet_score import compute as compute_composite

    return compute_composite(m, validation_win_rate=0.85, validation_gap=0.05)
