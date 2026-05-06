"""v0.5.6 Phase B — CategoryMatchingGate.

Signal-time gate: a wallet's bet should only be copied if the *market
category* belongs to the wallet's positive-EV category set. A
politics-specialist who randomly bets on a sports market shouldn't be
copied into that bet — the edge is category-bound.

The gate is *advisory* in v0.5.6 (logged, not enforced) until the user
flips ``CATEGORY_MATCHING_GATE_ENFORCED=true`` in settings. Until then
it surfaces a ``category_match`` boolean on every signal.

Decision surface::

    GateOutcome(
        allowed: bool,
        category: str,
        wallet_category_ev: float | None,
        wallet_category_sample: int,
        reason: "OK" | "NEGATIVE_CAT_EV" | "MISSING_CATEGORY" | "INSUFFICIENT_SAMPLE",
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GateOutcome:
    allowed: bool
    category: str
    wallet_category_ev: float | None
    wallet_category_sample: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "category": self.category,
            "wallet_category_ev": self.wallet_category_ev,
            "wallet_category_sample": self.wallet_category_sample,
            "reason": self.reason,
        }


def evaluate(
    market_category: str | None,
    category_edge_map: dict[str, dict[str, Any]] | None,
    *,
    min_sample: int = 5,
    min_ev: float = 0.0,
) -> GateOutcome:
    """Decide whether the signal should be copied based on the wallet's
    historical edge in this category.
    """
    cat = (market_category or "").strip() or "Uncategorised"
    info = (category_edge_map or {}).get(cat)
    if info is None:
        return GateOutcome(False, cat, None, 0, "MISSING_CATEGORY")
    sample = int(info.get("sample") or 0)
    ev = info.get("ev")
    if sample < min_sample:
        return GateOutcome(False, cat, ev, sample, "INSUFFICIENT_SAMPLE")
    if ev is None or ev <= min_ev:
        return GateOutcome(False, cat, ev, sample, "NEGATIVE_CAT_EV")
    return GateOutcome(True, cat, ev, sample, "OK")
