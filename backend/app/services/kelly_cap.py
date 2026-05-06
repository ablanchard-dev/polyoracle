"""v0.5.6 Phase B — Kelly cap risk profile.

A documentation/computation module that translates a wallet's
:class:`WalletEdgeMetrics` into a *recommended* per-trade risk fraction
of capital. Hard ceiling: 2 % of capital regardless of Kelly's optimal
output (matches the "0 faux ELITE" prudence stance).

Kelly formula for a binary bet with payoff ``b`` and win probability
``p``::

    f* = (p × b − (1 − p)) / b

We use ``ev_lower_bound`` as the conservative substitute for the point
EV — bet below the bootstrap p20, never above the median.

The result is purely informational in v0.5.6 — it is **not** wired into
the live trading path (still gated by ``LIVE_ENABLED=false``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.edge_quality_engine import WalletEdgeMetrics


KELLY_HARD_CAP = 0.02  # 2 % of capital — never exceeded.
KELLY_FRACTION = 0.25  # use 1/4 Kelly to add cushion.


@dataclass
class KellyRecommendation:
    address: str
    full_kelly_fraction: float | None
    quarter_kelly_fraction: float | None
    capped_fraction: float
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "full_kelly_fraction": self.full_kelly_fraction,
            "quarter_kelly_fraction": self.quarter_kelly_fraction,
            "capped_fraction": self.capped_fraction,
            "rationale": self.rationale,
        }


def recommend(metrics: WalletEdgeMetrics) -> KellyRecommendation:
    """Return a per-trade risk fraction in [0, KELLY_HARD_CAP]."""
    p_lb = None
    b = None
    if metrics.average_win_payout and metrics.average_loss_cost is not None:
        b = metrics.average_win_payout
    if metrics.win_rate is not None:
        p_lb = metrics.win_rate
    # Use ev_lower_bound to compute the implied p_lb if we have a payoff.
    if metrics.ev_lower_bound is not None and b and b > 0:
        # ev_lb = p_lb × b − (1 − p_lb) → solve for p_lb at ev_lb.
        p_implied = (metrics.ev_lower_bound + 1) / (b + 1)
        if 0 < p_implied < 1:
            p_lb = p_implied
    if p_lb is None or b is None or b <= 0:
        return KellyRecommendation(
            address=metrics.address,
            full_kelly_fraction=None,
            quarter_kelly_fraction=None,
            capped_fraction=0.0,
            rationale="missing_inputs",
        )
    full = (p_lb * b - (1 - p_lb)) / b
    if full <= 0:
        return KellyRecommendation(
            address=metrics.address,
            full_kelly_fraction=round(full, 4),
            quarter_kelly_fraction=0.0,
            capped_fraction=0.0,
            rationale="negative_kelly",
        )
    quarter = full * KELLY_FRACTION
    capped = min(quarter, KELLY_HARD_CAP)
    return KellyRecommendation(
        address=metrics.address,
        full_kelly_fraction=round(full, 4),
        quarter_kelly_fraction=round(quarter, 4),
        capped_fraction=round(capped, 4),
        rationale="ok" if capped == quarter else "hard_cap_2pct",
    )
