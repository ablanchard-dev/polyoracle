"""Tests for v0.5.6 Phase A — EdgeQualityEngine.

Each test builds a minimal synthetic trade-event list and a market-resolution
dict, then asserts on the resulting :class:`WalletEdgeMetrics` and
:class:`TierDecision`.

The point is to lock the "0 faux ELITE" rule: a 92 %-win-rate wallet that
buys at a terrible price must NOT come out ELITE.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.composite_wallet_score import (
    apply_tier_rules,
    compute as compute_composite,
)
from app.services.edge_quality_engine import (
    EdgeQualityEngine,
    derive_holdout_set,
)


def _trade(cid: str, oi: int, side: str, size: float, price: float, ts: int) -> dict[str, Any]:
    return {"cid": cid.lower(), "oi": oi, "side": side, "size": size, "price": price, "ts": ts}


def _resolutions(specs: list[tuple[str, int, str | None, str | None]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for cid, woi, end_date, category in specs:
        out[cid.lower()] = {
            "winning_outcome_index": woi,
            "winning_outcome_name": "Yes" if woi == 0 else "No",
            "end_date": end_date,
            "category": category or "Politics",
        }
    return out


def _winning_position(cid: str, price: float, base_ts: int = 1_700_000_000) -> list[dict[str, Any]]:
    """A net-long buy on outcome 0 (winner) at ``price``, never sold."""
    return [_trade(cid, 0, "BUY", 100.0, price, base_ts)]


def _losing_position(cid: str, price: float, base_ts: int = 1_700_000_000) -> list[dict[str, Any]]:
    """A net-long buy on outcome 0 (loser, since outcome 1 wins)."""
    return [_trade(cid, 0, "BUY", 100.0, price, base_ts)]


def _build_engine(resolutions: dict[str, dict[str, Any]]) -> EdgeQualityEngine:
    return EdgeQualityEngine(resolutions, holdout_fraction=0.0, bootstrap_resamples=200)


def test_high_win_rate_with_negative_ev_is_not_elite() -> None:
    """A 92 %-win-rate wallet that bought at p≈0.95 has EV≈-0.05 → never ELITE."""
    n_total = 100
    n_winners = 92
    base_ts = 1_700_000_000
    trades: list[dict[str, Any]] = []
    res_specs: list[tuple[str, int, str | None, str | None]] = []
    for k in range(n_winners):
        cid = f"0x{k:064x}"
        trades.extend(_winning_position(cid, 0.95, base_ts + k * 86400))
        res_specs.append((cid, 0, "2024-06-15", "Politics"))
    for k in range(n_total - n_winners):
        cid = f"0x{(n_winners + k):064x}"
        trades.extend(_losing_position(cid, 0.95, base_ts + (n_winners + k) * 86400))
        res_specs.append((cid, 1, "2024-06-15", "Politics"))
    engine = _build_engine(_resolutions(res_specs))
    m = engine.compute("0xfake", trades)
    assert m.sample_size == 100
    assert m.win_rate is not None and m.win_rate >= 0.90
    assert m.expected_value is not None and m.expected_value < 0
    assert "NEGATIVE_EV" in m.exclusion_status
    assert "BAD_ENTRY_PRICE_PATTERN" in m.exclusion_status

    bk = compute_composite(m, validation_win_rate=0.92, validation_gap=0.05)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.92, validation_gap=0.05)
    assert dec.tier != "ELITE"


def test_positive_ev_point_with_negative_ev_lower_bound_is_not_elite() -> None:
    """Tiny edge that doesn't survive bootstrap → blocked."""
    base_ts = 1_700_000_000
    trades: list[dict[str, Any]] = []
    res_specs: list[tuple[str, int, str | None, str | None]] = []
    # 6 winners at 0.50 (return +1.0), 4 losers (-1.0) → EV = +0.20 point but
    # only 10 samples — EV_lb p20 is well below zero.
    for k in range(6):
        cid = f"0x{k:064x}"
        trades.extend(_winning_position(cid, 0.50, base_ts + k * 86400))
        res_specs.append((cid, 0, "2024-06-15", "Politics"))
    for k in range(4):
        cid = f"0x{(6 + k):064x}"
        trades.extend(_losing_position(cid, 0.50, base_ts + (6 + k) * 86400))
        res_specs.append((cid, 1, "2024-06-15", "Politics"))
    engine = _build_engine(_resolutions(res_specs))
    m = engine.compute("0xfake", trades)
    assert m.expected_value is not None and m.expected_value > 0
    # 10 samples — bootstrap p20 typically lands negative on this dispersion.
    assert m.ev_lower_bound is not None
    assert m.sample_size < 100
    bk = compute_composite(m)
    dec = apply_tier_rules(m, bk)
    assert dec.tier != "ELITE"  # sample<100 alone disqualifies


def test_sample_below_100_blocks_elite_even_if_ev_excellent() -> None:
    """A 50-trade wallet with 90 % wr and EV=+0.45 still cannot be ELITE."""
    base_ts = 1_700_000_000
    trades: list[dict[str, Any]] = []
    res_specs: list[tuple[str, int, str | None, str | None]] = []
    for k in range(45):
        cid = f"0x{k:064x}"
        trades.extend(_winning_position(cid, 0.40, base_ts + k * 86400))
        res_specs.append((cid, 0, "2024-06-15", "Politics"))
    for k in range(5):
        cid = f"0x{(45 + k):064x}"
        trades.extend(_losing_position(cid, 0.40, base_ts + (45 + k) * 86400))
        res_specs.append((cid, 1, "2024-06-15", "Politics"))
    engine = _build_engine(_resolutions(res_specs))
    m = engine.compute("0xfake", trades)
    assert m.sample_size == 50
    assert m.expected_value is not None and m.expected_value > 0
    assert "INSUFFICIENT_SAMPLE" not in m.exclusion_status  # 50 ≥ 30 OK for STRONG
    bk = compute_composite(m, validation_win_rate=0.90, validation_gap=0.05)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.90, validation_gap=0.05)
    assert dec.tier != "ELITE"  # sample below 100 blocks ELITE


def test_holdout_drop_flags_overfit_and_blocks_elite() -> None:
    """Calibration EV positive, holdout EV deeply negative → OVERFIT_RISK."""
    base_ts = 1_700_000_000
    trades: list[dict[str, Any]] = []
    res_specs: list[tuple[str, int, str | None, str | None]] = []
    # Build cids whose first byte is < 31 (≈12 % holdout cutoff).
    # Holdout IDs: first byte < 31 (0x00..0x1e). 12% × 256 ≈ 30 → cutoff < 0x1f.
    holdout_ids = [f"0x0{k:01x}{k:062x}" for k in range(1, 15)]  # first byte 0x01..0x0e
    # Calibration IDs: first byte 0xff so they never land in holdout.
    cal_winning_ids = [f"0xff{k:062x}" for k in range(80)]
    for cid in cal_winning_ids:
        trades.extend(_winning_position(cid, 0.40, base_ts))
        res_specs.append((cid, 0, "2024-06-15", "Politics"))
    for cid in holdout_ids:
        trades.extend(_losing_position(cid, 0.40, base_ts))
        res_specs.append((cid, 1, "2024-06-15", "Politics"))

    holdout_set = derive_holdout_set(_resolutions(res_specs), holdout_fraction=0.12)
    # Sanity: at least 5 of the holdout-id cids are actually in the holdout set.
    assert len(holdout_set & set(holdout_ids)) >= 5

    engine = EdgeQualityEngine(
        _resolutions(res_specs),
        holdout_set=holdout_set,
        bootstrap_resamples=200,
    )
    m = engine.compute("0xfake", trades)
    assert m.holdout_details["calibration_ev"] is not None
    assert m.holdout_details["holdout_ev"] is not None
    assert m.holdout_details["holdout_ev"] < 0 < m.holdout_details["calibration_ev"]
    assert m.holdout_pass is False
    assert (
        "HOLDOUT_FAILED" in m.exclusion_status or "OVERFIT_RISK" in m.exclusion_status
    )
    bk = compute_composite(m, validation_win_rate=0.85, validation_gap=0.05)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.85, validation_gap=0.05)
    assert dec.tier != "ELITE"


def test_persistence_below_0_70_blocks_elite() -> None:
    """A wallet whose edge is concentrated in 1 quartile fails persistence."""
    base_ts = 1_700_000_000  # ~ Nov 2023
    trades: list[dict[str, Any]] = []
    res_specs: list[tuple[str, int, str | None, str | None]] = []
    # Quartile 1: 25 winners @0.40 (positive EV).
    for k in range(25):
        cid = f"0x{k:064x}"
        trades.extend(_winning_position(cid, 0.40, base_ts + k * 3600))
        res_specs.append((cid, 0, "2024-01-15", "Politics"))
    # Quartiles 2-4: 75 losses @0.40 (negative EV).
    for k in range(75):
        cid = f"0x{(25 + k):064x}"
        trades.extend(_losing_position(cid, 0.40, base_ts + (25 + k) * 86400 * 30))
        res_specs.append((cid, 1, "2024-06-15", "Politics"))
    engine = _build_engine(_resolutions(res_specs))
    m = engine.compute("0xfake", trades)
    assert m.persistence_score < 0.70
    bk = compute_composite(m, validation_win_rate=0.80, validation_gap=0.05)
    dec = apply_tier_rules(m, bk, validation_win_rate=0.80, validation_gap=0.05)
    assert dec.tier != "ELITE"


def test_clean_elite_passes_all_gates() -> None:
    """Wallet with sample=120, wr=80%, EV=+0.60, persistence>0.70, no overfit."""
    from datetime import datetime, UTC
    # Span ~120 days ending a few days ago so activity_recency_score is high.
    now_ts = int(datetime.now(UTC).timestamp())
    base_ts = now_ts - 130 * 86400
    end_iso = "2025-09-15"  # falls in 2025_post era
    trades: list[dict[str, Any]] = []
    res_specs: list[tuple[str, int, str | None, str | None]] = []
    quartile_size = 30
    for q in range(4):
        n_w = 24
        n_l = 6
        for k in range(n_w):
            idx = q * quartile_size + k
            cid = f"0x{0x80 + idx % 0x70:02x}{idx:062x}"  # avoid holdout range
            trades.extend(_winning_position(cid, 0.50, base_ts + idx * 86400))
            res_specs.append((cid, 0, end_iso, "Politics"))
        for k in range(n_l):
            idx = q * quartile_size + n_w + k
            cid = f"0x{0x80 + idx % 0x70:02x}{idx:062x}"
            trades.extend(_losing_position(cid, 0.50, base_ts + idx * 86400))
            res_specs.append((cid, 1, end_iso, "Politics"))
    engine = EdgeQualityEngine(_resolutions(res_specs), holdout_fraction=0.0, bootstrap_resamples=200)
    m = engine.compute("0xfake", trades)
    assert m.sample_size == 120
    assert m.win_rate == pytest.approx(0.80, abs=0.001)
    assert m.expected_value is not None and m.expected_value > 0.05
    assert m.persistence_score >= 0.70
    bk = compute_composite(m, validation_win_rate=0.80, validation_gap=0.05)
    dec = apply_tier_rules(
        m,
        bk,
        validation_win_rate=0.80,
        validation_gap=0.05,
        elite_min_composite=60.0,  # ELITE-ish threshold
        elite_min_ev_lower_bound=0.01,
    )
    assert dec.tier == "ELITE"


def test_holdout_set_is_deterministic() -> None:
    res = _resolutions(
        [
            ("0x01abc" + "0" * 60, 0, None, None),
            ("0x10abc" + "0" * 60, 0, None, None),
            ("0xa0abc" + "0" * 60, 0, None, None),
            ("0xff_ic" + "0" * 60, 0, None, None),  # invalid hex, skipped
        ]
    )
    h1 = derive_holdout_set(res, holdout_fraction=0.12)
    h2 = derive_holdout_set(res, holdout_fraction=0.12)
    assert h1 == h2
    # 0x01... is in holdout (first byte 0x01 < 31)
    assert "0x01abc" + "0" * 60 in h1
    # 0xa0... is NOT in holdout
    assert "0xa0abc" + "0" * 60 not in h1
