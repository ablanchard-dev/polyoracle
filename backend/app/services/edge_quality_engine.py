"""v0.5.6 Phase A — EdgeQualityEngine.

Computes per-wallet edge metrics from REAL entry prices, with
EV_lower_bound (bootstrap), persistence (4-quartile split), holdout
test, category edge map, position-size sanity, regime robustness and
activity-recency scoring.

The engine is pure compute — it does NOT fetch /activity. The caller
must pass already-normalised trade events (see ``Trade`` typed shape
below) plus a market-resolutions dict.

Trade event shape::

    {"cid": "<conditionId lowercased>", "oi": <outcome_index int>,
     "side": "BUY" | "SELL", "size": <float shares>,
     "price": <float 0..1 entry price>, "ts": <int unix seconds>}

Market resolution shape::

    {"<cid lowercased>": {"winning_outcome_index": <int>,
                          "end_date": <iso8601 str | None>,
                          "category": <str | None>}, ...}

Returned dataclass: :class:`WalletEdgeMetrics`.

Statuses surfaced under ``exclusion_status`` (any present → wallet
cannot be ELITE; some are also STRONG-blockers, see
``BLOCK_ANY_TIER_STATUSES``):

* NEGATIVE_EV                — point EV < 0
* EV_LOWER_BOUND_NEGATIVE    — bootstrap p20 EV < 0
* BAD_ENTRY_PRICE_PATTERN    — avg entry price > 0.85 with EV ≤ 0.02
* OVERFIT_RISK               — holdout EV drop > 30 %
* HOLDOUT_FAILED             — holdout sample ≥ 5 + holdout EV negative
* CATEGORY_EDGE_NEGATIVE     — every traded category with sample ≥ 5 has EV ≤ 0
* INSUFFICIENT_SAMPLE        — < 30 resolved positions
* STALE_WALLET               — last trade > 26 weeks ago
* BAD_POSITION_SIZE_PATTERN  — single market > 50 % of total notional, or CV(notional) > 3
* REGIME_FRAGILE             — < 50 % of usable eras have positive EV
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Statuses that block ELITE _and_ STRONG (vs ELITE-only).
BLOCK_ANY_TIER_STATUSES: frozenset[str] = frozenset(
    {
        "NEGATIVE_EV",
        "EV_LOWER_BOUND_NEGATIVE",
        "BAD_ENTRY_PRICE_PATTERN",
        "HOLDOUT_FAILED",
        "INSUFFICIENT_SAMPLE",
    }
)
BLOCK_ELITE_ONLY_STATUSES: frozenset[str] = frozenset(
    {
        "OVERFIT_RISK",
        "CATEGORY_EDGE_NEGATIVE",
        "STALE_WALLET",
        "BAD_POSITION_SIZE_PATTERN",
        "REGIME_FRAGILE",
    }
)


ERAS: list[tuple[str, str | None, str | None]] = [
    ("2020_2022_low_volume", None, "2023-01-01"),
    ("2023_build_up", "2023-01-01", "2024-01-01"),
    ("2024_pre_election", "2024-01-01", "2024-07-01"),
    ("2024_election_surge", "2024-07-01", "2025-01-01"),
    ("2025_post", "2025-01-01", None),
]


@dataclass
class WalletEdgeMetrics:
    address: str
    sample_size: int  # resolved net-long positions (winners + losers)
    n_winners: int
    n_losers: int
    win_rate: float | None
    average_entry_price: float | None
    average_win_payout: float | None  # mean per-dollar return on winners
    average_loss_cost: float | None  # mean per-dollar return on losers (≈ −1)
    expected_value: float | None  # point estimate
    ev_lower_bound: float | None  # bootstrap p20
    ev_confidence: str  # INSUFFICIENT_DATA | LOW | MEDIUM | HIGH
    risk_reward_ratio: float | None  # avg_win / |avg_loss|
    breakeven_win_rate: float | None  # win rate needed for EV=0 at observed avg_payoff
    price_entry_sanity: str  # OK | HIGH_PRICES | LOW_PRICES | MIXED | UNKNOWN
    persistence_score: float  # 0..1 fraction of usable quartiles with EV>0
    persistence_details: dict[str, Any]
    holdout_pass: bool
    holdout_details: dict[str, Any]
    category_edge_map: dict[str, dict[str, Any]]
    regime_robustness: dict[str, Any]
    activity_recency_weeks: float | None
    activity_recency_score: float  # 0..1
    position_size_sanity: dict[str, Any]
    temporal_profile: dict[str, Any]
    avg_position_notional_usd: float
    total_notional_usd: float
    weeks_observed: float | None
    estimated_trades_per_week: float | None
    expected_weekly_R: float | None
    exclusion_status: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "sample_size": self.sample_size,
            "n_winners": self.n_winners,
            "n_losers": self.n_losers,
            "win_rate": self.win_rate,
            "average_entry_price": self.average_entry_price,
            "average_win_payout": self.average_win_payout,
            "average_loss_cost": self.average_loss_cost,
            "expected_value": self.expected_value,
            "ev_lower_bound": self.ev_lower_bound,
            "ev_confidence": self.ev_confidence,
            "risk_reward_ratio": self.risk_reward_ratio,
            "breakeven_win_rate": self.breakeven_win_rate,
            "price_entry_sanity": self.price_entry_sanity,
            "persistence_score": self.persistence_score,
            "persistence_details": self.persistence_details,
            "holdout_pass": self.holdout_pass,
            "holdout_details": self.holdout_details,
            "category_edge_map": self.category_edge_map,
            "regime_robustness": self.regime_robustness,
            "activity_recency_weeks": self.activity_recency_weeks,
            "activity_recency_score": self.activity_recency_score,
            "position_size_sanity": self.position_size_sanity,
            "temporal_profile": self.temporal_profile,
            "avg_position_notional_usd": self.avg_position_notional_usd,
            "total_notional_usd": self.total_notional_usd,
            "weeks_observed": self.weeks_observed,
            "estimated_trades_per_week": self.estimated_trades_per_week,
            "expected_weekly_R": self.expected_weekly_R,
            "exclusion_status": list(self.exclusion_status),
            "notes": list(self.notes),
        }


def _safe_div(a: float, b: float, default: float | None = None) -> float | None:
    if b == 0:
        return default
    return a / b


def _aggregate_positions(trades: Iterable[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """Group trade events by (conditionId, outcomeIndex) and accumulate
    buy/sell size + notional and first/last timestamps.
    """
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for tr in trades:
        cid = tr.get("cid")
        if not cid:
            continue
        oi = tr.get("oi")
        if oi is None:
            continue
        side = (tr.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        size = float(tr.get("size") or 0.0)
        price = float(tr.get("price") or 0.0)
        ts = int(tr.get("ts") or 0)
        bucket = out.get((cid, int(oi)))
        if bucket is None:
            bucket = {
                "buys_size": 0.0,
                "buys_notional": 0.0,
                "sells_size": 0.0,
                "sells_notional": 0.0,
                "trades_count": 0,
                "first_ts": ts,
                "last_ts": ts,
            }
            out[(cid, int(oi))] = bucket
        bucket["trades_count"] += 1
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


def _per_position_records(
    positions: dict[tuple[str, int], dict[str, Any]],
    market_resolutions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert positions into closed records with per-dollar return."""
    records: list[dict[str, Any]] = []
    for (cid, oi), pos in positions.items():
        net = pos["buys_size"] - pos["sells_size"]
        if net <= 0:
            continue  # not net long → can't attribute resolution PnL
        if pos["buys_size"] <= 0:
            continue
        avg_buy_price = pos["buys_notional"] / pos["buys_size"]
        if avg_buy_price <= 0 or avg_buy_price >= 1:
            continue  # degenerate
        resolution = market_resolutions.get(cid.lower() if isinstance(cid, str) else cid)
        if resolution is None:
            continue
        woi = resolution.get("winning_outcome_index")
        if woi is None:
            continue
        is_winner = int(oi) == int(woi)
        ret = (1.0 - avg_buy_price) / avg_buy_price if is_winner else -1.0
        records.append(
            {
                "cid": cid,
                "oi": oi,
                "is_winner": is_winner,
                "avg_buy_price": avg_buy_price,
                "return_per_dollar": ret,
                "notional": pos["buys_notional"],
                "first_ts": pos["first_ts"],
                "last_ts": pos["last_ts"],
                "category": (resolution.get("category") or "Uncategorised"),
                "end_date": resolution.get("end_date"),
            }
        )
    return records


def _bootstrap_ev_lower_bound(
    returns: list[float],
    n_resamples: int = 1000,
    percentile: int = 20,
    seed: int = 42,
) -> float | None:
    """Bootstrap a one-sided lower-bound estimator of the mean.

    Resamples ``returns`` with replacement ``n_resamples`` times, computes
    the resample mean each time, and returns the requested percentile —
    a conservative point below which the true mean is unlikely to sit.
    Returns ``None`` when sample size is too small (<5) for a stable
    estimate.
    """
    n = len(returns)
    if n < 5:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        s = 0.0
        for _i in range(n):
            s += returns[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    idx = max(0, int(len(means) * percentile / 100.0) - 1)
    return means[idx]


def _confidence_from_sample(sample: int) -> str:
    if sample < 5:
        return "INSUFFICIENT_DATA"
    if sample < 30:
        return "LOW"
    if sample < 100:
        return "MEDIUM"
    return "HIGH"


def _compute_persistence(records: list[dict[str, Any]], n_quartiles: int = 4) -> tuple[float, dict[str, Any]]:
    """Sort records by entry timestamp and split into ``n_quartiles``.
    Compute mean return per quartile (require ≥3 samples). The
    persistence score is the fraction of usable quartiles with EV>0.
    """
    if len(records) < n_quartiles * 3:
        return 0.0, {
            "usable_quartiles": 0,
            "n_required": n_quartiles * 3,
            "n_observed": len(records),
            "per_quartile": [],
        }
    by_ts = sorted(records, key=lambda r: r.get("first_ts") or 0)
    chunk = len(by_ts) // n_quartiles
    per_quartile: list[dict[str, Any]] = []
    usable = 0
    positives = 0
    for q in range(n_quartiles):
        start = q * chunk
        end = (q + 1) * chunk if q < n_quartiles - 1 else len(by_ts)
        slab = by_ts[start:end]
        if len(slab) < 3:
            per_quartile.append({"q": q, "sample": len(slab), "ev": None, "skipped": True})
            continue
        rets = [r["return_per_dollar"] for r in slab]
        ev = sum(rets) / len(rets)
        per_quartile.append({"q": q, "sample": len(slab), "ev": round(ev, 4), "skipped": False})
        usable += 1
        if ev > 0:
            positives += 1
    persistence = positives / usable if usable else 0.0
    return persistence, {
        "usable_quartiles": usable,
        "positive_quartiles": positives,
        "per_quartile": per_quartile,
    }


def _compute_holdout(
    records: list[dict[str, Any]],
    holdout_set: frozenset[str],
    overfit_drop_threshold: float = 0.30,
) -> tuple[bool, dict[str, Any]]:
    """Split records into calibration (~88 %) and holdout (~12 %) using
    a deterministic market-id partition (provided by caller). Compare
    mean EV. Pass = (holdout_sample < 5) OR (holdout_ev ≥ 0 AND drop ≤ 30 %).
    """
    cal_rets: list[float] = []
    hol_rets: list[float] = []
    for r in records:
        bucket = hol_rets if (r["cid"] in holdout_set) else cal_rets
        bucket.append(r["return_per_dollar"])
    cal_n = len(cal_rets)
    hol_n = len(hol_rets)
    cal_ev = sum(cal_rets) / cal_n if cal_n else None
    hol_ev = sum(hol_rets) / hol_n if hol_n else None
    if hol_n < 5 or cal_ev is None:
        # Not enough holdout data — neutral pass.
        return True, {
            "calibration_sample": cal_n,
            "calibration_ev": cal_ev,
            "holdout_sample": hol_n,
            "holdout_ev": hol_ev,
            "drop": None,
            "verdict": "NEUTRAL_INSUFFICIENT_HOLDOUT",
        }
    drop = None
    if cal_ev != 0:
        drop = (cal_ev - hol_ev) / abs(cal_ev) if cal_ev != 0 else None
    overfit = drop is not None and drop > overfit_drop_threshold
    holdout_negative = hol_ev < 0 and cal_ev > 0
    passed = not overfit and not holdout_negative
    verdict = "PASS"
    if overfit:
        verdict = "OVERFIT_RISK"
    elif holdout_negative:
        verdict = "HOLDOUT_FAILED"
    return passed, {
        "calibration_sample": cal_n,
        "calibration_ev": round(cal_ev, 4),
        "holdout_sample": hol_n,
        "holdout_ev": round(hol_ev, 4) if hol_ev is not None else None,
        "drop": round(drop, 4) if drop is not None else None,
        "verdict": verdict,
    }


def _compute_category_edge_map(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        cat = r.get("category") or "Uncategorised"
        by_cat.setdefault(cat, []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for cat, rows in by_cat.items():
        rets = [r["return_per_dollar"] for r in rows]
        wins = sum(1 for r in rows if r["is_winner"])
        ev = sum(rets) / len(rets) if rets else None
        out[cat] = {
            "sample": len(rows),
            "win_rate": round(wins / len(rows), 4) if rows else None,
            "ev": round(ev, 4) if ev is not None else None,
        }
    return out


def _parse_iso(date_str: str | None) -> datetime | None:
    """Always return a UTC-aware datetime (or None) so era comparisons stay safe."""
    if not date_str:
        return None
    s = date_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(s.split("T")[0])
        except Exception:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _compute_regime_robustness(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_era: dict[str, list[float]] = {name: [] for (name, _, _) in ERAS}
    for r in records:
        end = _parse_iso(r.get("end_date"))
        if end is None:
            continue
        for name, lo, hi in ERAS:
            lo_dt = _parse_iso(lo) if lo else None
            hi_dt = _parse_iso(hi) if hi else None
            if (lo_dt is None or end >= lo_dt) and (hi_dt is None or end < hi_dt):
                by_era[name].append(r["return_per_dollar"])
                break
    per_era: list[dict[str, Any]] = []
    usable_eras = 0
    positive_eras = 0
    for name in by_era:
        rets = by_era[name]
        if len(rets) < 5:
            per_era.append({"era": name, "sample": len(rets), "ev": None, "usable": False})
            continue
        ev = sum(rets) / len(rets)
        per_era.append({"era": name, "sample": len(rets), "ev": round(ev, 4), "usable": True})
        usable_eras += 1
        if ev > 0:
            positive_eras += 1
    fraction = positive_eras / usable_eras if usable_eras else None
    return {
        "usable_eras": usable_eras,
        "positive_eras": positive_eras,
        "fraction_positive": round(fraction, 4) if fraction is not None else None,
        "per_era": per_era,
    }


def _compute_position_size_sanity(records: list[dict[str, Any]]) -> dict[str, Any]:
    notionals = [r["notional"] for r in records if r["notional"] > 0]
    if not notionals:
        return {
            "avg_notional": 0.0,
            "median_notional": 0.0,
            "max_share_single_market": 0.0,
            "cv_notional": None,
            "is_sane": False,
            "reason": "no_notional_data",
        }
    notionals_sorted = sorted(notionals)
    mid = len(notionals_sorted) // 2
    median_n = (
        notionals_sorted[mid]
        if len(notionals_sorted) % 2 == 1
        else (notionals_sorted[mid - 1] + notionals_sorted[mid]) / 2
    )
    total = sum(notionals)
    max_n = max(notionals)
    max_share = max_n / total if total else 0.0
    avg_n = total / len(notionals)
    var = sum((n - avg_n) ** 2 for n in notionals) / len(notionals)
    std = var**0.5
    cv = std / avg_n if avg_n > 0 else None
    reasons: list[str] = []
    if avg_n < 10:
        reasons.append("avg_below_10usd")
    if avg_n > 100_000:
        reasons.append("avg_above_100k_usd")
    if cv is not None and cv > 3:
        reasons.append(f"cv_notional_{cv:.2f}_above_3")
    if max_share > 0.5:
        reasons.append(f"single_market_share_{max_share:.2f}_above_0.5")
    return {
        "avg_notional": round(avg_n, 2),
        "median_notional": round(median_n, 2),
        "max_share_single_market": round(max_share, 4),
        "cv_notional": round(cv, 4) if cv is not None else None,
        "is_sane": not reasons,
        "reason": ",".join(reasons) if reasons else "ok",
    }


def _compute_temporal_profile(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "earliest_ts": None,
            "latest_ts": None,
            "weeks_observed": None,
            "estimated_trades_per_week": None,
            "burstiness_score": None,
            "markets_per_week": None,
        }
    timestamps = sorted(r["first_ts"] for r in records if r.get("first_ts"))
    if not timestamps:
        return {
            "earliest_ts": None,
            "latest_ts": None,
            "weeks_observed": None,
            "estimated_trades_per_week": None,
            "burstiness_score": None,
            "markets_per_week": None,
        }
    earliest, latest = timestamps[0], timestamps[-1]
    seconds = max(1, latest - earliest)
    weeks = max(0.5, seconds / (7 * 86400))
    markets_per_week = len(records) / weeks
    # Burstiness: coefficient of variation of inter-trade gaps.
    gaps = [timestamps[k + 1] - timestamps[k] for k in range(len(timestamps) - 1)]
    burst: float | None = None
    if len(gaps) >= 5:
        mean_gap = sum(gaps) / len(gaps)
        var_gap = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
        std_gap = var_gap**0.5
        if mean_gap > 0:
            burst = round(std_gap / mean_gap, 4)
    return {
        "earliest_ts": earliest,
        "latest_ts": latest,
        "weeks_observed": round(weeks, 2),
        "estimated_trades_per_week": round(markets_per_week, 4),
        "burstiness_score": burst,
        "markets_per_week": round(markets_per_week, 4),
    }


def _compute_price_entry_sanity(
    avg_entry_price: float | None,
    expected_value: float | None,
) -> str:
    if avg_entry_price is None or expected_value is None:
        return "UNKNOWN"
    high = avg_entry_price > 0.85
    low = avg_entry_price < 0.15
    if high and expected_value <= 0.02:
        # Wins frequently but at terrible entry price → real-world EV is bad.
        return "HIGH_PRICES"
    if low and expected_value <= 0:
        return "LOW_PRICES"
    if 0.15 <= avg_entry_price <= 0.85:
        return "OK"
    return "MIXED"


def _activity_recency_score(weeks_since_last: float | None) -> tuple[float, float | None]:
    """Continuous decay: 1.0 at <2 weeks, 0.0 at >=26 weeks, linear between."""
    if weeks_since_last is None:
        return 0.0, None
    if weeks_since_last <= 2:
        return 1.0, weeks_since_last
    if weeks_since_last >= 26:
        return 0.0, weeks_since_last
    # Linear taper between 2 and 26 weeks.
    return round(1.0 - (weeks_since_last - 2) / (26 - 2), 4), weeks_since_last


def derive_holdout_set(
    market_resolutions: dict[str, dict[str, Any]],
    holdout_fraction: float = 0.12,
) -> frozenset[str]:
    """Deterministic market-id holdout — uses lowercase hex prefix so the
    same markets land in holdout regardless of run order.
    """
    if holdout_fraction <= 0 or not market_resolutions:
        return frozenset()
    cutoff = int(holdout_fraction * 256)
    selected: set[str] = set()
    for cid in market_resolutions:
        if not isinstance(cid, str) or len(cid) < 4:
            continue
        try:
            byte = int(cid[2:4], 16)  # "0x" prefix → first byte
        except ValueError:
            continue
        if byte < cutoff:
            selected.add(cid)
    return frozenset(selected)


class EdgeQualityEngine:
    """Pure compute over cached trade events + market resolutions."""

    def __init__(
        self,
        market_resolutions: dict[str, dict[str, Any]],
        *,
        holdout_set: frozenset[str] | None = None,
        holdout_fraction: float = 0.12,
        bootstrap_resamples: int = 1000,
        bootstrap_percentile: int = 20,
        ev_lower_bound_block_threshold: float = 0.0,
        bad_entry_price_threshold: float = 0.85,
        bad_entry_price_ev_floor: float = 0.02,
        category_edge_min_sample: int = 5,
        regime_min_fraction_positive: float = 0.5,
        position_size_min_avg_usd: float = 10.0,
        position_size_max_avg_usd: float = 100_000.0,
        position_size_max_cv: float = 3.0,
        position_size_max_market_share: float = 0.5,
    ) -> None:
        self.market_resolutions = market_resolutions
        self.holdout_set = holdout_set if holdout_set is not None else derive_holdout_set(
            market_resolutions, holdout_fraction
        )
        self.bootstrap_resamples = bootstrap_resamples
        self.bootstrap_percentile = bootstrap_percentile
        self.ev_lower_bound_block_threshold = ev_lower_bound_block_threshold
        self.bad_entry_price_threshold = bad_entry_price_threshold
        self.bad_entry_price_ev_floor = bad_entry_price_ev_floor
        self.category_edge_min_sample = category_edge_min_sample
        self.regime_min_fraction_positive = regime_min_fraction_positive
        self.position_size_min_avg_usd = position_size_min_avg_usd
        self.position_size_max_avg_usd = position_size_max_avg_usd
        self.position_size_max_cv = position_size_max_cv
        self.position_size_max_market_share = position_size_max_market_share

    def compute(self, address: str, trades: Iterable[dict[str, Any]]) -> WalletEdgeMetrics:
        positions = _aggregate_positions(trades)
        records = _per_position_records(positions, self.market_resolutions)
        sample = len(records)
        winners = [r for r in records if r["is_winner"]]
        losers = [r for r in records if not r["is_winner"]]
        n_w = len(winners)
        n_l = len(losers)

        if sample == 0:
            return self._empty(address)

        win_rate = n_w / sample
        avg_payoff = sum(r["return_per_dollar"] for r in winners) / n_w if n_w else None
        avg_loss = sum(r["return_per_dollar"] for r in losers) / n_l if n_l else None
        avg_entry = sum(r["avg_buy_price"] * r["notional"] for r in records) / max(
            1e-9, sum(r["notional"] for r in records)
        )

        ev = (
            (n_w / sample) * (avg_payoff if avg_payoff is not None else 0.0)
            + (n_l / sample) * (avg_loss if avg_loss is not None else 0.0)
        )
        if n_w == 0 and avg_loss is not None:
            ev = avg_loss
        if n_l == 0 and avg_payoff is not None:
            ev = avg_payoff

        all_returns = [r["return_per_dollar"] for r in records]
        ev_lb = _bootstrap_ev_lower_bound(
            all_returns,
            n_resamples=self.bootstrap_resamples,
            percentile=self.bootstrap_percentile,
        )
        rr = (
            avg_payoff / abs(avg_loss)
            if (avg_payoff is not None and avg_loss is not None and avg_loss != 0)
            else None
        )
        breakeven_wr = 1.0 / (1.0 + avg_payoff) if avg_payoff and avg_payoff > 0 else None

        persistence_score, persistence_details = _compute_persistence(records)
        holdout_pass, holdout_details = _compute_holdout(records, self.holdout_set)
        category_edge_map = _compute_category_edge_map(records)
        regime = _compute_regime_robustness(records)
        position_sanity = _compute_position_size_sanity(records)
        temporal = _compute_temporal_profile(records)

        latest_ts = temporal.get("latest_ts")
        weeks_since: float | None = None
        if latest_ts:
            seconds = max(0, int(datetime.now(UTC).timestamp()) - int(latest_ts))
            weeks_since = round(seconds / (7 * 86400), 4)
        recency_score, recency_weeks = _activity_recency_score(weeks_since)

        price_sanity = _compute_price_entry_sanity(avg_entry, ev)

        notionals_total = sum(r["notional"] for r in records)
        avg_position = notionals_total / sample if sample else 0.0
        weeks_obs = temporal.get("weeks_observed")
        trades_per_week = temporal.get("estimated_trades_per_week")
        weekly_R = ev * trades_per_week if (ev is not None and trades_per_week is not None) else None

        exclusions: list[str] = []
        notes: list[str] = []
        if sample < 30:
            exclusions.append("INSUFFICIENT_SAMPLE")
        if ev is not None and ev < 0:
            exclusions.append("NEGATIVE_EV")
        if ev_lb is not None and ev_lb < self.ev_lower_bound_block_threshold:
            exclusions.append("EV_LOWER_BOUND_NEGATIVE")
        if (
            avg_entry is not None
            and avg_entry > self.bad_entry_price_threshold
            and (ev is None or ev <= self.bad_entry_price_ev_floor)
        ):
            exclusions.append("BAD_ENTRY_PRICE_PATTERN")
        verdict = holdout_details.get("verdict")
        if verdict == "OVERFIT_RISK":
            exclusions.append("OVERFIT_RISK")
        if verdict == "HOLDOUT_FAILED":
            exclusions.append("HOLDOUT_FAILED")
        if category_edge_map:
            usable_cats = [
                v for v in category_edge_map.values() if (v.get("sample") or 0) >= self.category_edge_min_sample
            ]
            if usable_cats and all((c.get("ev") or 0) <= 0 for c in usable_cats):
                exclusions.append("CATEGORY_EDGE_NEGATIVE")
        if weeks_since is not None and weeks_since > 26:
            exclusions.append("STALE_WALLET")
        if not position_sanity.get("is_sane"):
            exclusions.append("BAD_POSITION_SIZE_PATTERN")
        if regime.get("usable_eras") and (
            (regime.get("fraction_positive") or 0) < self.regime_min_fraction_positive
        ):
            exclusions.append("REGIME_FRAGILE")

        return WalletEdgeMetrics(
            address=address,
            sample_size=sample,
            n_winners=n_w,
            n_losers=n_l,
            win_rate=round(win_rate, 4),
            average_entry_price=round(avg_entry, 4),
            average_win_payout=round(avg_payoff, 4) if avg_payoff is not None else None,
            average_loss_cost=round(avg_loss, 4) if avg_loss is not None else None,
            expected_value=round(ev, 4) if ev is not None else None,
            ev_lower_bound=round(ev_lb, 4) if ev_lb is not None else None,
            ev_confidence=_confidence_from_sample(sample),
            risk_reward_ratio=round(rr, 4) if rr is not None else None,
            breakeven_win_rate=round(breakeven_wr, 4) if breakeven_wr is not None else None,
            price_entry_sanity=price_sanity,
            persistence_score=round(persistence_score, 4),
            persistence_details=persistence_details,
            holdout_pass=holdout_pass,
            holdout_details=holdout_details,
            category_edge_map=category_edge_map,
            regime_robustness=regime,
            activity_recency_weeks=recency_weeks,
            activity_recency_score=recency_score,
            position_size_sanity=position_sanity,
            temporal_profile=temporal,
            avg_position_notional_usd=round(avg_position, 2),
            total_notional_usd=round(notionals_total, 2),
            weeks_observed=weeks_obs,
            estimated_trades_per_week=trades_per_week,
            expected_weekly_R=round(weekly_R, 4) if weekly_R is not None else None,
            exclusion_status=exclusions,
            notes=notes,
        )

    def _empty(self, address: str) -> WalletEdgeMetrics:
        return WalletEdgeMetrics(
            address=address,
            sample_size=0,
            n_winners=0,
            n_losers=0,
            win_rate=None,
            average_entry_price=None,
            average_win_payout=None,
            average_loss_cost=None,
            expected_value=None,
            ev_lower_bound=None,
            ev_confidence="INSUFFICIENT_DATA",
            risk_reward_ratio=None,
            breakeven_win_rate=None,
            price_entry_sanity="UNKNOWN",
            persistence_score=0.0,
            persistence_details={"usable_quartiles": 0, "per_quartile": []},
            holdout_pass=True,
            holdout_details={"verdict": "NEUTRAL_NO_DATA"},
            category_edge_map={},
            regime_robustness={"usable_eras": 0, "per_era": []},
            activity_recency_weeks=None,
            activity_recency_score=0.0,
            position_size_sanity={"is_sane": False, "reason": "no_data"},
            temporal_profile={
                "weeks_observed": None,
                "estimated_trades_per_week": None,
                "burstiness_score": None,
            },
            avg_position_notional_usd=0.0,
            total_notional_usd=0.0,
            weeks_observed=None,
            estimated_trades_per_week=None,
            expected_weekly_R=None,
            exclusion_status=["INSUFFICIENT_SAMPLE"],
            notes=["no_resolved_positions"],
        )
