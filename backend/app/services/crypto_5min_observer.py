"""v0.7.6 B14 — REPORT-ONLY observation of crypto_5min markets.

NOT a strategy. Just a probe that records pipeline characteristics
on markets matching crypto_5min patterns. Future activation requires
adaptive polling top-wallets <= 30s and measured latency p95 < 60-90s.

Detection criteria (any of):
- ``category == 'Crypto'`` AND market.duration_minutes <= 10
- ``raw_slug`` contains '5min' / '5minute' / '5-min'
- ``question`` matches BTC/ETH up/down 5m patterns

This module is observation-only — never modifies pipeline behaviour.
"""

from __future__ import annotations

import re
from typing import Any

_PATTERN_SLUG = re.compile(r"5\s*-?\s*min", re.IGNORECASE)
_PATTERN_BTC_ETH_5M = re.compile(
    r"\b(btc|bitcoin|eth|ethereum|sol|solana)\b.*\b(5\s?min|5\s?minute|next\s?5)\b",
    re.IGNORECASE,
)


def is_crypto_5min(
    *,
    category: str | None,
    slug: str | None = None,
    question: str | None = None,
    duration_minutes: float | None = None,
) -> bool:
    """Return True iff this market looks like a 5-minute crypto market."""
    cat = (category or "").strip().lower()
    if cat == "crypto" and duration_minutes is not None and 0 < duration_minutes <= 10:
        return True
    if slug and _PATTERN_SLUG.search(slug):
        return True
    if question and _PATTERN_BTC_ETH_5M.search(question):
        return True
    return False


class Crypto5minObserver:
    """In-memory accumulator for crypto_5min metrics during a session.

    Caller pushes a record each time the polling pipeline processes a
    trade. The observer NEVER changes the trade flow. At the end of the
    session, ``snapshot()`` produces the report dict.
    """

    def __init__(self) -> None:
        self.detected_count = 0
        self.rejected_late_entry = 0
        self.spreads: list[float] = []
        self.fees: list[float] = []
        self.ev_after_fee_values: list[float] = []
        self.copy_delays_seconds: list[float] = []
        self.price_deteriorations: list[float] = []
        self.latencies_ms: list[float] = []

    def record(
        self,
        *,
        is_match: bool,
        rejected_reason: str | None = None,
        spread: float | None = None,
        fee_amount: float | None = None,
        ev_after_fee_value: float | None = None,
        copy_delay_seconds: float | None = None,
        price_deterioration: float | None = None,
        latency_ms: float | None = None,
    ) -> None:
        if not is_match:
            return
        self.detected_count += 1
        if rejected_reason and "LATE_ENTRY" in (rejected_reason or "").upper():
            self.rejected_late_entry += 1
        if spread is not None:
            self.spreads.append(float(spread))
        if fee_amount is not None:
            self.fees.append(float(fee_amount))
        if ev_after_fee_value is not None:
            self.ev_after_fee_values.append(float(ev_after_fee_value))
        if copy_delay_seconds is not None:
            self.copy_delays_seconds.append(float(copy_delay_seconds))
        if price_deterioration is not None:
            self.price_deteriorations.append(float(price_deterioration))
        if latency_ms is not None:
            self.latencies_ms.append(float(latency_ms))

    @staticmethod
    def _percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        s = sorted(values)
        if p <= 0:
            return s[0]
        if p >= 1:
            return s[-1]
        k = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
        return s[k]

    def snapshot(self) -> dict[str, Any]:
        """Render current accumulator state as a JSON-friendly dict.

        Returned by smoke orchestrators for the v0.7.6 report. Values are
        ``None`` when the sample is empty (no observations yet)."""
        return {
            "crypto_5min_detected_count": self.detected_count,
            "rejected_by_late_entry": self.rejected_late_entry,
            "latency_p50_ms": self._percentile(self.latencies_ms, 0.50),
            "latency_p95_ms": self._percentile(self.latencies_ms, 0.95),
            "latency_max_ms": self._percentile(self.latencies_ms, 1.0),
            "spread_avg": (sum(self.spreads) / len(self.spreads)) if self.spreads else None,
            "fee_avg_usdc": (sum(self.fees) / len(self.fees)) if self.fees else None,
            "ev_after_fee_avg": (sum(self.ev_after_fee_values) / len(self.ev_after_fee_values)) if self.ev_after_fee_values else None,
            "ev_after_fee_p50": self._percentile(self.ev_after_fee_values, 0.50),
            "ev_after_fee_p95": self._percentile(self.ev_after_fee_values, 0.95),
            "copy_delay_avg_s": (sum(self.copy_delays_seconds) / len(self.copy_delays_seconds)) if self.copy_delays_seconds else None,
            "price_deterioration_avg": (sum(self.price_deteriorations) / len(self.price_deteriorations)) if self.price_deteriorations else None,
            "n_obs_spread": len(self.spreads),
            "n_obs_fee": len(self.fees),
            "n_obs_ev_after_fee": len(self.ev_after_fee_values),
            "n_obs_latency": len(self.latencies_ms),
            "active_strategy": False,  # always False per spec
            "report_only_mode": True,
        }
