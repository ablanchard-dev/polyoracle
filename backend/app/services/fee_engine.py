"""v0.7.6 B12 — Polymarket dynamic fee engine.

Implements the official formula::

    fee = C * feeRate * p * (1 - p)

where ``C`` is the trade quantity (USDC equivalent), ``feeRate`` is the
per-category taker fee, and ``p`` is the entry price.

Notes:
- Fee absolute value is **maximal at p=0.50** (p×(1-p) = 0.25), minimal
  at extremes (p=0.05 or p=0.95).
- Fee as % of notional decreases with p (= rate × (1-p)).
- Maker rebate up to 25% applies if the fill is maker. We do NOT count it
  by default (paper trading assumes taker / market order).
- Geopolitics is FREE (rate=0). Crypto is most expensive (rate=0.072).

This module is pure: no DB, no IO, deterministic.
"""

from __future__ import annotations

# Polymarket category fee schedule (taker rates).
# Keys lowercased for canonical lookup.
FEE_RATES: dict[str, float] = {
    "crypto": 0.072,
    "sports": 0.030,
    "finance": 0.040,
    "politics": 0.040,
    "economics": 0.050,
    "culture": 0.050,
    "weather": 0.050,
    "mentions": 0.040,
    "tech": 0.040,
    "geopolitics": 0.000,  # FREE
    "other": 0.050,
    "unknown": 0.050,  # conservative fallback
}

# Maker rebate by category (in % of paid fee). Documented but NOT applied
# by default — paper trading assumes taker fills.
MAKER_REBATE_RATES: dict[str, float] = {
    "crypto": 0.20,
    "sports": 0.25,
    "finance": 0.25,
    "politics": 0.25,
    "economics": 0.25,
    "culture": 0.25,
    "weather": 0.25,
    "mentions": 0.25,
    "tech": 0.25,
    "other": 0.25,
    "unknown": 0.25,
}

# High-fee zone (max abs fee around p=0.50). Trades inside this band incur
# a small copyable_edge malus per spec.
HIGH_FEE_ZONE_LOW = 0.45
HIGH_FEE_ZONE_HIGH = 0.55
HIGH_FEE_ZONE_EDGE_PENALTY = 0.005  # subtract 0.5pp from edge in this band

# Low-fee zone (extremes — min abs fee). NO bonus is applied (per spec :
# "sans inventer d'edge"); just informational.
LOW_FEE_ZONE_LOW = 0.30
LOW_FEE_ZONE_HIGH = 0.70

DEFAULT_FEE_RATE = 0.050


_CATEGORY_ALIASES: dict[str, str] = {
    "political": "politics",
    "geopolitical": "geopolitics",
    "macro": "economics",
    "macroeconomics": "economics",
    "general": "other",
    "btc": "crypto",
    "eth": "crypto",
    "election": "politics",
}


def _canonical_category(category: str | None) -> str:
    """Normalize a category string to one of the canonical keys."""
    if not category:
        return "unknown"
    norm = category.strip().lower()
    return _CATEGORY_ALIASES.get(norm, norm)


def get_fee_rate(category: str | None) -> float:
    """Return the canonical taker fee rate for a category. Unknown
    categories fall back to the conservative 5% default."""
    if not category:
        return DEFAULT_FEE_RATE
    canon = _canonical_category(category)
    return FEE_RATES.get(canon, DEFAULT_FEE_RATE)


def compute_dynamic_fee(category: str | None, price: float, size: float) -> float:
    """Compute the absolute Polymarket taker fee in USDC for a single
    fill at price ``p`` of size ``C`` shares.

    fee = size × feeRate × p × (1 - p)

    Returns 0.0 for invalid inputs (size<=0, price<=0 or >=1).
    """
    if size <= 0 or price <= 0 or price >= 1:
        return 0.0
    rate = get_fee_rate(category)
    return float(size) * rate * float(price) * (1.0 - float(price))


def compute_fee_pct_of_notional(category: str | None, price: float) -> float:
    """Fee as a fraction of notional value (= C × p) at given price.
    Independent of size. Used for filter heuristics.

    fee_pct_of_notional = feeRate × (1 - p)
    """
    if price <= 0 or price >= 1:
        return 0.0
    rate = get_fee_rate(category)
    return rate * (1.0 - float(price))


def is_high_fee_zone(price: float) -> bool:
    """True when the price sits in [0.45, 0.55] — the highest-absolute-fee band."""
    return HIGH_FEE_ZONE_LOW <= float(price) <= HIGH_FEE_ZONE_HIGH


def is_low_fee_zone(price: float) -> bool:
    """True when the price sits below 0.30 or above 0.70 — the
    lowest-absolute-fee bands."""
    p = float(price)
    return p < LOW_FEE_ZONE_LOW or p > LOW_FEE_ZONE_HIGH


def edge_after_dynamic_fee(
    *, copyable_edge: float, category: str | None, price: float
) -> float:
    """Adjust copyable_edge for the high-fee zone malus. Low-fee zone gets
    no bonus per spec.

    Returns the adjusted edge — never negative.
    """
    edge = float(copyable_edge)
    if is_high_fee_zone(price):
        edge -= HIGH_FEE_ZONE_EDGE_PENALTY
    return max(0.0, edge)


def ev_after_fee(
    *,
    ev_lower_bound: float,
    category: str | None,
    price: float,
    size: float,
) -> float:
    """Compute EV after deducting the dynamic Polymarket fee.

    The fee is normalized per share by dividing by ``size`` so the result
    keeps the same units as ev_lower_bound (a per-share EV).
    """
    fee_amount = compute_dynamic_fee(category, price, size)
    if size <= 0:
        return float(ev_lower_bound)
    return float(ev_lower_bound) - (fee_amount / size)
