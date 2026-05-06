"""v0.5.8 — tests for the LostGemsReAudit pipeline.

Verifies:
* Negative-EV gem after dense recompute → does NOT promote.
* EV-positive + EV_lb-positive gem → promotes to STRONG via recompute.
* k-fold method requires every fold to be positive.
* Bootstrap method only applies in the 30-99 sample window.
"""

from __future__ import annotations

from typing import Any

from app.services.lost_gems_re_audit import (
    method_bootstrap,
    method_kfold,
    method_recompute,
    re_audit_wallet,
)


def _winning_position(cid: str, oi: int, price: float, ts: int) -> dict[str, Any]:
    return {
        "cid": cid,
        "oi": oi,
        "side": "BUY",
        "size": 100.0,
        "price": price,
        "ts": ts,
        "wallet": "0xfake",
    }


def _build_synthetic_records(n_winners: int, n_losers: int, win_price: float = 0.40, loss_price: float = 0.40) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    res: dict[str, dict[str, Any]] = {}
    base_ts = 1_700_000_000
    for k in range(n_winners):
        cid = f"0x{k:064x}"
        trades.append(_winning_position(cid, 0, win_price, base_ts + k * 86400))
        res[cid.lower()] = {"winning_outcome_index": 0}
    for k in range(n_losers):
        cid = f"0x{(n_winners + k):064x}"
        trades.append(_winning_position(cid, 0, loss_price, base_ts + (n_winners + k) * 86400))
        res[cid.lower()] = {"winning_outcome_index": 1}  # outcome 0 lost
    return trades, res


def test_lost_gem_with_negative_EV_after_dense_does_not_promote() -> None:
    """A wallet that wins 60% at 0.95 (avg entry) → EV ≈ 0.6 × 0.053 − 0.4 × 1 = −0.37 → reject."""
    trades, res = _build_synthetic_records(60, 40, win_price=0.95, loss_price=0.95)
    decision = re_audit_wallet("0xfake", trades, res)
    assert decision.final_tier == "REJECTED"
    assert decision.method_used is None


def test_lost_gem_with_validated_EV_lb_promotes_to_strong() -> None:
    """80 wins / 20 losses at 0.40 entry → EV positive, EV_lb positive."""
    trades, res = _build_synthetic_records(80, 20, win_price=0.40, loss_price=0.40)
    decision = re_audit_wallet("0xfake", trades, res)
    assert decision.final_tier in {"STRONG_EV_VALIDATED", "ELITE_EV_VALIDATED"}
    assert decision.method_used in {"recompute", "kfold", "bootstrap"}
    assert decision.ev_lower_bound is not None and decision.ev_lower_bound > 0


def test_kfold_rejects_when_one_fold_is_negative() -> None:
    """Build a sample where the 5th fold is all losers."""
    trades: list[dict[str, Any]] = []
    res: dict[str, dict[str, Any]] = {}
    base_ts = 1_700_000_000
    # Fold 1-4: 24 wins each (96 wins).
    # Fold 5: 24 losses. Total 120 records.
    for q in range(4):
        for k in range(24):
            cid = f"0x{q * 100 + k:064x}"
            trades.append(_winning_position(cid, 0, 0.40, base_ts + (q * 24 + k) * 86400))
            res[cid.lower()] = {"winning_outcome_index": 0}
    for k in range(24):
        cid = f"0xff{k:062x}"
        trades.append(_winning_position(cid, 0, 0.40, base_ts + (96 + k) * 86400))
        res[cid.lower()] = {"winning_outcome_index": 1}  # outcome 0 lost
    # Build positions/records via the engine helpers so kfold sees them as the engine would.
    from app.services.lost_gems_re_audit import (
        aggregate_positions_from_dense,
        positions_to_records,
    )
    positions = aggregate_positions_from_dense(trades)
    records = positions_to_records(positions, res)
    result = method_kfold(records)
    # 5th fold has min ev_lb very negative → rejected.
    assert result["verdict"] in {"REJECTED_KFOLD_THRESHOLDS", "REJECTED_FOLD_DATA"}


def test_bootstrap_method_only_applies_to_30_to_99_sample() -> None:
    # Sample 50 — within range.
    trades_in, res_in = _build_synthetic_records(40, 10, win_price=0.40, loss_price=0.40)
    from app.services.lost_gems_re_audit import (
        aggregate_positions_from_dense,
        positions_to_records,
    )
    records_in = positions_to_records(aggregate_positions_from_dense(trades_in), res_in)
    result = method_bootstrap(records_in)
    assert result["verdict"] in {"PROMOTE_STRONG", "REJECTED_BOOTSTRAP_THRESHOLDS"}
    # Sample 200 — out of range.
    trades_out, res_out = _build_synthetic_records(160, 40)
    records_out = positions_to_records(aggregate_positions_from_dense(trades_out), res_out)
    assert method_bootstrap(records_out)["verdict"] == "REJECTED_SAMPLE_OUT_OF_RANGE"
    # Sample 10 — below.
    trades_low, res_low = _build_synthetic_records(8, 2)
    records_low = positions_to_records(aggregate_positions_from_dense(trades_low), res_low)
    assert method_bootstrap(records_low)["verdict"] == "REJECTED_SAMPLE_OUT_OF_RANGE"


def test_recompute_blocks_bad_entry_pattern_even_if_winrate_high() -> None:
    """High win rate at very high prices → BAD_ENTRY_PRICE_PATTERN, no promotion."""
    trades, res = _build_synthetic_records(95, 5, win_price=0.95, loss_price=0.95)
    from app.services.lost_gems_re_audit import (
        aggregate_positions_from_dense,
        positions_to_records,
    )
    records = positions_to_records(aggregate_positions_from_dense(trades), res)
    result = method_recompute(records)
    assert result["verdict"] in {
        "REJECTED_BAD_ENTRY_PRICE",
        "REJECTED_NEGATIVE_EV",
        "REJECTED_EV_LB_NEGATIVE",
    }


def test_zero_resolved_positions_returns_rejected() -> None:
    decision = re_audit_wallet("0xfake", [], {})
    assert decision.final_tier == "REJECTED"
    assert decision.sample_size == 0
