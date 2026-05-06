"""v0.5.6 Phase B — Wallet decay & revalidation.

Applies time-based decay to the composite score so a wallet that hasn't
traded in a while loses its allowed_safe / allowed_aggressive privileges
even if its historical edge is strong.

Rules:

* −5 composite-score points per 14-day inactivity window (linear).
* Hard floor: composite never goes below 0.
* When ``activity_recency_weeks > 26``, status flips to ELITE_STALE /
  STRONG_STALE / etc. and ``allowed_safe`` is revoked.
* When ``activity_recency_weeks > 52``, the entry is moved to
  REVALIDATION_REQUIRED and disallowed everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DECAY_PER_WINDOW_POINTS = 5.0
DECAY_WINDOW_WEEKS = 2.0  # 14 days
STALE_AFTER_WEEKS = 26.0
REVALIDATION_AFTER_WEEKS = 52.0


@dataclass
class DecayedScore:
    address: str
    base_composite: float
    decayed_composite: float
    decay_applied: float
    weeks_since_last_trade: float | None
    status_suffix: str  # "" | "_STALE" | "_REVALIDATION_REQUIRED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "base_composite": self.base_composite,
            "decayed_composite": self.decayed_composite,
            "decay_applied": self.decay_applied,
            "weeks_since_last_trade": self.weeks_since_last_trade,
            "status_suffix": self.status_suffix,
        }


def apply_decay(
    address: str,
    base_composite: float,
    weeks_since_last_trade: float | None,
) -> DecayedScore:
    if weeks_since_last_trade is None or weeks_since_last_trade <= DECAY_WINDOW_WEEKS:
        return DecayedScore(
            address=address,
            base_composite=base_composite,
            decayed_composite=base_composite,
            decay_applied=0.0,
            weeks_since_last_trade=weeks_since_last_trade,
            status_suffix="",
        )
    windows = max(0.0, (weeks_since_last_trade - DECAY_WINDOW_WEEKS) / DECAY_WINDOW_WEEKS)
    decay = windows * DECAY_PER_WINDOW_POINTS
    decayed = max(0.0, base_composite - decay)
    suffix = ""
    if weeks_since_last_trade > REVALIDATION_AFTER_WEEKS:
        suffix = "_REVALIDATION_REQUIRED"
    elif weeks_since_last_trade > STALE_AFTER_WEEKS:
        suffix = "_STALE"
    return DecayedScore(
        address=address,
        base_composite=round(base_composite, 2),
        decayed_composite=round(decayed, 2),
        decay_applied=round(decay, 2),
        weeks_since_last_trade=weeks_since_last_trade,
        status_suffix=suffix,
    )


def revoke_allowed_safe(weeks_since_last_trade: float | None) -> bool:
    """Return True if allowed_safe must be stripped for staleness."""
    return weeks_since_last_trade is not None and weeks_since_last_trade > STALE_AFTER_WEEKS
