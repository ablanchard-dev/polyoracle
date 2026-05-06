"""v0.5.6 Phase B — Copyable edge with dynamic entry.

When a smart wallet enters a market at price ``p_w``, the copy bot
sees the trade with some delay. The market may have already moved to
``p_c > p_w``. The copyable edge is::

    edge_at_wallet      = (1 - p_w) / p_w        (winning case)
    edge_at_copy        = (1 - p_c) / p_c
    edge_retained_ratio = edge_at_copy / edge_at_wallet

A copy is allowed only if ``edge_retained_ratio >= min_retained``.

Also enforces a hard ceiling on the copy entry price (so we never
chase past 0.95 even if the wallet's edge looks intact).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CopyableEdge:
    wallet_entry_price: float
    copy_entry_price: float
    wallet_edge: float
    copy_edge: float
    edge_retained_ratio: float
    copy_price_deterioration: float  # (p_c - p_w) / p_w
    max_acceptable_copy_price: float
    copy_allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet_entry_price": self.wallet_entry_price,
            "copy_entry_price": self.copy_entry_price,
            "wallet_edge": self.wallet_edge,
            "copy_edge": self.copy_edge,
            "edge_retained_ratio": self.edge_retained_ratio,
            "copy_price_deterioration": self.copy_price_deterioration,
            "max_acceptable_copy_price": self.max_acceptable_copy_price,
            "copy_allowed": self.copy_allowed,
            "reason": self.reason,
        }


def evaluate(
    wallet_entry_price: float,
    copy_entry_price: float,
    *,
    min_retained: float = 0.50,
    max_copy_price: float = 0.95,
) -> CopyableEdge:
    if wallet_entry_price <= 0 or wallet_entry_price >= 1:
        return CopyableEdge(
            wallet_entry_price,
            copy_entry_price,
            0.0,
            0.0,
            0.0,
            0.0,
            max_copy_price,
            False,
            "INVALID_WALLET_PRICE",
        )
    if copy_entry_price <= 0 or copy_entry_price >= 1:
        return CopyableEdge(
            wallet_entry_price,
            copy_entry_price,
            0.0,
            0.0,
            0.0,
            0.0,
            max_copy_price,
            False,
            "INVALID_COPY_PRICE",
        )
    wallet_edge = (1.0 - wallet_entry_price) / wallet_entry_price
    copy_edge = (1.0 - copy_entry_price) / copy_entry_price
    retained = copy_edge / wallet_edge if wallet_edge > 0 else 0.0
    deterioration = (copy_entry_price - wallet_entry_price) / wallet_entry_price
    if copy_entry_price > max_copy_price:
        return CopyableEdge(
            wallet_entry_price,
            copy_entry_price,
            wallet_edge,
            copy_edge,
            retained,
            deterioration,
            max_copy_price,
            False,
            "COPY_PRICE_ABOVE_HARD_CEILING",
        )
    if retained < min_retained:
        return CopyableEdge(
            wallet_entry_price,
            copy_entry_price,
            wallet_edge,
            copy_edge,
            retained,
            deterioration,
            max_copy_price,
            False,
            f"EDGE_RETAINED_RATIO_{retained:.2f}_BELOW_{min_retained:.2f}",
        )
    return CopyableEdge(
        wallet_entry_price,
        copy_entry_price,
        wallet_edge,
        copy_edge,
        retained,
        deterioration,
        max_copy_price,
        True,
        "OK",
    )
