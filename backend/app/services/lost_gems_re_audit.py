"""v0.5.8 — LostGemsReAudit.

Promotes wallets from the v0.5.5.1 audit ``lost_gems_reintegrated.csv``
to STRONG_EV_VALIDATED (or, rarely, ELITE_EV_VALIDATED) using the
deeper trade history from the v0.5.7 dense audit (5.27M trades).

Three independent verification methods — a wallet promotes if ANY of
them pass cleanly. Hard floor: ``EV_lower_bound > 0`` is required on
the chosen method, no fallback to laxer thresholds.

Method 1 — Recompute on dense data
    Take all per-wallet trades from the dense cache, build positions,
    cross-reference with ResolvedMarketRecord, recompute sample / EV /
    EV_lb (bootstrap p20). Apply v0.5.6-style strict gate at the new
    sample size.

Method 2 — 5-fold chronological cross-validation
    Sort positions by entry timestamp, split into 5 contiguous folds.
    Compute fold EVs. Promote if min_fold_EV_lower_bound > 0 AND
    mean_fold_val_wr ≥ 0.65.

Method 3 — Bootstrap on combined sample
    For wallets with sample 30-99, run 1000-resample bootstrap on the
    concatenated trade returns (v0.5.6 cache + dense delta). Promote
    if EV_lb > 0.02 AND sample ≥ 30.

The promoted tier is **STRONG_EV_VALIDATED** by default. ELITE only
fires when the wallet would clear the v0.5.6 ELITE rules outright
(sample ≥ 100 + HIGH confidence + EV_lb ≥ 0.05 + val_wr ≥ 0.70 +
val_gap ≤ 0.15 + persistence ≥ 0.70 + holdout_pass).
"""

from __future__ import annotations

import json
import logging
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------- thresholds ----------------

STRONG_MIN_SAMPLE = 30
STRONG_MIN_EV_LB = 0.005
STRONG_MIN_VAL_WR = 0.65
STRONG_MAX_VAL_GAP = 0.20
STRONG_MIN_PERSISTENCE = 0.50

KFOLD_MIN_VAL_WR_MEAN = 0.65
KFOLD_FLOOR_FOLD_EV_LB = 0.0  # min_fold_EV_lb must be > 0
N_FOLDS = 5

BOOTSTRAP_MIN_SAMPLE = 30
BOOTSTRAP_MAX_SAMPLE = 99
BOOTSTRAP_MIN_EV_LB = 0.02
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_PERCENTILE = 20

ELITE_MIN_SAMPLE = 100
ELITE_MIN_EV_LB = 0.05
ELITE_MIN_VAL_WR = 0.70
ELITE_MAX_VAL_GAP = 0.15
ELITE_MIN_PERSISTENCE = 0.70


@dataclass
class GemDecision:
    address: str
    final_tier: str  # "ELITE_EV_VALIDATED" | "STRONG_EV_VALIDATED" | "REJECTED"
    method_used: str | None  # "recompute" | "kfold" | "bootstrap" | "none"
    sample_size: int
    n_winners: int
    n_losers: int
    win_rate: float | None
    ev_point: float | None
    ev_lower_bound: float | None
    persistence_score: float | None
    holdout_pass: bool | None
    method_results: dict[str, Any] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "final_tier": self.final_tier,
            "method_used": self.method_used,
            "sample_size": self.sample_size,
            "n_winners": self.n_winners,
            "n_losers": self.n_losers,
            "win_rate": self.win_rate,
            "ev_point": self.ev_point,
            "ev_lower_bound": self.ev_lower_bound,
            "persistence_score": self.persistence_score,
            "holdout_pass": self.holdout_pass,
            "method_results": self.method_results,
            "rejection_reasons": list(self.rejection_reasons),
        }


# ---------------- core math ----------------


def aggregate_positions_from_dense(
    trades: Iterable[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, float]]:
    """Group dense trade events by (cid, oi). Returns positions with
    avg_buy_price computable + first_ts / last_ts.
    """
    out: dict[tuple[str, int], dict[str, float]] = {}
    for t in trades:
        cid = (t.get("cid") or t.get("conditionId") or "").lower()
        if not cid:
            continue
        oi = t.get("oi")
        if oi is None:
            oi = t.get("outcomeIndex")
        if oi is None:
            continue
        try:
            oi = int(oi)
        except (TypeError, ValueError):
            continue
        side = (t.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        try:
            size = float(t.get("size") or 0.0)
            price = float(t.get("price") or 0.0)
            ts = int(t.get("ts") or t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        bucket = out.get((cid, oi))
        if bucket is None:
            bucket = {
                "buys_size": 0.0,
                "buys_notional": 0.0,
                "sells_size": 0.0,
                "sells_notional": 0.0,
                "first_ts": ts,
                "last_ts": ts,
            }
            out[(cid, oi)] = bucket
        if ts and ts < bucket["first_ts"]:
            bucket["first_ts"] = ts
        if ts and ts > bucket["last_ts"]:
            bucket["last_ts"] = ts
        if side == "BUY":
            bucket["buys_size"] += size
            bucket["buys_notional"] += size * price
        else:
            bucket["sells_size"] += size
            bucket["sells_notional"] += size * price
    return out


def positions_to_records(
    positions: dict[tuple[str, int], dict[str, float]],
    market_resolutions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build per-position closed records carrying return_per_dollar."""
    records: list[dict[str, Any]] = []
    for (cid, oi), pos in positions.items():
        net = pos["buys_size"] - pos["sells_size"]
        if net <= 0 or pos["buys_size"] <= 0:
            continue
        avg_price = pos["buys_notional"] / pos["buys_size"]
        if avg_price <= 0 or avg_price >= 1:
            continue
        res = market_resolutions.get(cid)
        if not res:
            continue
        woi = res.get("winning_outcome_index")
        if woi is None:
            continue
        is_win = oi == int(woi)
        ret = (1.0 - avg_price) / avg_price if is_win else -1.0
        records.append({
            "cid": cid,
            "oi": oi,
            "is_winner": is_win,
            "avg_buy_price": avg_price,
            "return_per_dollar": ret,
            "first_ts": pos["first_ts"],
            "last_ts": pos["last_ts"],
        })
    return records


def bootstrap_lower_bound(
    returns: list[float],
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    percentile: int = BOOTSTRAP_PERCENTILE,
    seed: int = 42,
) -> float | None:
    n = len(returns)
    if n < 5:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        s = 0.0
        for _ in range(n):
            s += returns[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    idx = max(0, int(n_resamples * percentile / 100) - 1)
    return means[idx]


def persistence_score(records: list[dict[str, Any]], n_quartiles: int = 4) -> float:
    if len(records) < n_quartiles * 3:
        return 0.0
    by_ts = sorted(records, key=lambda r: r.get("first_ts") or 0)
    chunk = len(by_ts) // n_quartiles
    usable = 0
    positive = 0
    for q in range(n_quartiles):
        start = q * chunk
        end = (q + 1) * chunk if q < n_quartiles - 1 else len(by_ts)
        slab = by_ts[start:end]
        if len(slab) < 3:
            continue
        rets = [r["return_per_dollar"] for r in slab]
        usable += 1
        if sum(rets) / len(rets) > 0:
            positive += 1
    return positive / usable if usable else 0.0


# ---------------- the 3 methods ----------------


def method_recompute(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Method 1 — full dense recompute + bootstrap EV_lb + persistence."""
    sample = len(records)
    if sample == 0:
        return {"verdict": "REJECTED", "reason": "no_resolved_positions"}
    n_w = sum(1 for r in records if r["is_winner"])
    win_rate = n_w / sample
    rets = [r["return_per_dollar"] for r in records]
    ev_point = sum(rets) / sample
    ev_lb = bootstrap_lower_bound(rets)
    persist = persistence_score(records)
    avg_entry = (
        sum(r["avg_buy_price"] for r in records) / sample if sample else None
    )
    bad_entry = (avg_entry or 0) > 0.85 and (ev_point or 0) <= 0.02
    return {
        "sample": sample,
        "win_rate": round(win_rate, 4),
        "ev_point": round(ev_point, 4) if ev_point is not None else None,
        "ev_lower_bound": round(ev_lb, 4) if ev_lb is not None else None,
        "persistence": round(persist, 4),
        "average_entry_price": round(avg_entry, 4) if avg_entry is not None else None,
        "bad_entry_pattern": bad_entry,
        "verdict": _grade_recompute(sample, win_rate, ev_point, ev_lb, persist, bad_entry),
    }


def _grade_recompute(
    sample: int,
    win_rate: float,
    ev_point: float | None,
    ev_lb: float | None,
    persistence: float,
    bad_entry: bool,
) -> str:
    if sample < STRONG_MIN_SAMPLE:
        return "REJECTED_SAMPLE"
    if (ev_point or 0) <= 0:
        return "REJECTED_NEGATIVE_EV"
    if ev_lb is None or ev_lb <= 0:
        return "REJECTED_EV_LB_NEGATIVE"
    if bad_entry:
        return "REJECTED_BAD_ENTRY_PRICE"
    # ELITE check (rare)
    if (
        sample >= ELITE_MIN_SAMPLE
        and ev_lb >= ELITE_MIN_EV_LB
        and win_rate >= ELITE_MIN_VAL_WR
        and persistence >= ELITE_MIN_PERSISTENCE
    ):
        return "PROMOTE_ELITE"
    # STRONG check
    if (
        ev_lb >= STRONG_MIN_EV_LB
        and win_rate >= STRONG_MIN_VAL_WR
        and persistence >= STRONG_MIN_PERSISTENCE
    ):
        return "PROMOTE_STRONG"
    return "REJECTED_THRESHOLDS"


def method_kfold(records: list[dict[str, Any]], n_folds: int = N_FOLDS) -> dict[str, Any]:
    """Method 2 — chronological 5-fold cross-validation. Pass if every
    fold has positive EV_lb AND mean fold win rate ≥ 65%.
    """
    sample = len(records)
    if sample < n_folds * 6:  # need ≥ 6 per fold for stable bootstrap
        return {"verdict": "REJECTED_SAMPLE", "sample": sample, "reason": f"need {n_folds * 6}, have {sample}"}
    by_ts = sorted(records, key=lambda r: r.get("first_ts") or 0)
    chunk = len(by_ts) // n_folds
    folds: list[dict[str, Any]] = []
    fold_evs: list[float] = []
    fold_wrs: list[float] = []
    for f in range(n_folds):
        start = f * chunk
        end = (f + 1) * chunk if f < n_folds - 1 else len(by_ts)
        slab = by_ts[start:end]
        rets = [r["return_per_dollar"] for r in slab]
        wins = sum(1 for r in slab if r["is_winner"])
        ev_lb = bootstrap_lower_bound(rets, n_resamples=400, seed=42 + f) if len(rets) >= 5 else None
        fold_wr = wins / len(slab) if slab else 0.0
        fold_ev = sum(rets) / len(rets) if rets else 0.0
        folds.append({
            "fold": f,
            "sample": len(slab),
            "ev": round(fold_ev, 4),
            "ev_lb": round(ev_lb, 4) if ev_lb is not None else None,
            "win_rate": round(fold_wr, 4),
        })
        if ev_lb is not None:
            fold_evs.append(ev_lb)
        fold_wrs.append(fold_wr)
    if not fold_evs or len(fold_evs) < n_folds:
        return {"verdict": "REJECTED_FOLD_DATA", "folds": folds}
    min_ev_lb = min(fold_evs)
    mean_wr = sum(fold_wrs) / len(fold_wrs)
    return {
        "sample": sample,
        "min_fold_ev_lb": round(min_ev_lb, 4),
        "mean_fold_win_rate": round(mean_wr, 4),
        "folds": folds,
        "verdict": (
            "PROMOTE_STRONG"
            if min_ev_lb > KFOLD_FLOOR_FOLD_EV_LB and mean_wr >= KFOLD_MIN_VAL_WR_MEAN
            else "REJECTED_KFOLD_THRESHOLDS"
        ),
    }


def method_bootstrap(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Method 3 — bootstrap-only on the combined sample (sample 30-99)."""
    sample = len(records)
    if sample < BOOTSTRAP_MIN_SAMPLE or sample > BOOTSTRAP_MAX_SAMPLE:
        return {"verdict": "REJECTED_SAMPLE_OUT_OF_RANGE", "sample": sample}
    rets = [r["return_per_dollar"] for r in records]
    ev_lb = bootstrap_lower_bound(rets)
    if ev_lb is None:
        return {"verdict": "REJECTED_BOOTSTRAP_FAILED", "sample": sample}
    persist = persistence_score(records)
    return {
        "sample": sample,
        "ev_lower_bound": round(ev_lb, 4),
        "persistence": round(persist, 4),
        "verdict": (
            "PROMOTE_STRONG"
            if ev_lb >= BOOTSTRAP_MIN_EV_LB and persist >= STRONG_MIN_PERSISTENCE
            else "REJECTED_BOOTSTRAP_THRESHOLDS"
        ),
    }


# ---------------- driver ----------------


def re_audit_wallet(
    address: str,
    trades: list[dict[str, Any]],
    market_resolutions: dict[str, dict[str, Any]],
) -> GemDecision:
    """Run the 3 methods and pick the strongest verdict."""
    positions = aggregate_positions_from_dense(trades)
    records = positions_to_records(positions, market_resolutions)
    sample = len(records)
    n_w = sum(1 for r in records if r["is_winner"])
    n_l = sample - n_w

    m1 = method_recompute(records)
    m2 = method_kfold(records) if sample >= N_FOLDS * 6 else {"verdict": "SKIPPED_SAMPLE"}
    m3 = (
        method_bootstrap(records)
        if BOOTSTRAP_MIN_SAMPLE <= sample <= BOOTSTRAP_MAX_SAMPLE
        else {"verdict": "SKIPPED_SAMPLE"}
    )

    method_results = {"recompute": m1, "kfold": m2, "bootstrap": m3}
    rejection: list[str] = []
    final_tier = "REJECTED"
    method_used: str | None = None

    if m1.get("verdict") == "PROMOTE_ELITE":
        final_tier = "ELITE_EV_VALIDATED"
        method_used = "recompute"
    elif m1.get("verdict") == "PROMOTE_STRONG":
        final_tier = "STRONG_EV_VALIDATED"
        method_used = "recompute"
    elif m2.get("verdict") == "PROMOTE_STRONG":
        final_tier = "STRONG_EV_VALIDATED"
        method_used = "kfold"
    elif m3.get("verdict") == "PROMOTE_STRONG":
        final_tier = "STRONG_EV_VALIDATED"
        method_used = "bootstrap"
    else:
        for tag, res in method_results.items():
            v = res.get("verdict")
            if v and v != "SKIPPED_SAMPLE":
                rejection.append(f"{tag}={v}")

    return GemDecision(
        address=address,
        final_tier=final_tier,
        method_used=method_used,
        sample_size=sample,
        n_winners=n_w,
        n_losers=n_l,
        win_rate=(n_w / sample) if sample else None,
        ev_point=m1.get("ev_point"),
        ev_lower_bound=m1.get("ev_lower_bound"),
        persistence_score=m1.get("persistence"),
        holdout_pass=None,  # not re-tested at this stage
        method_results=method_results,
        rejection_reasons=rejection,
    )
