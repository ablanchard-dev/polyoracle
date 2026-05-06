"""Risk-mode policy registry — DEPRECATED in v0.7.2.

The bot no longer has named operational modes. The single runtime engine is
``CapitalAllocator`` with capital-aware sliding-scale tightening. The three
``RiskModeProfile`` constants (``SAFE`` / ``AGGRESSIVE`` / ``FULL_PAPER``)
are kept ONLY for legacy callers (``edge_validation_engine`` backtest
projections, ``risk_engine.validate_for_mode`` historical adapter, and the
matching tests in ``test_risk_modes.py``). Production code paths must use
``CapitalAllocator`` + the ``BASE_PAPER`` / ``BASE_LIVE`` presets in
``app/config/presets.yaml`` directly.

DO NOT add new callers to these constants. They will be removed once the
historical edge_validation backtest is rewritten to call CapitalAllocator
with capital tiers (50 / 500 / 5000) instead of legacy profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RiskModeName = Literal["SAFE", "AGGRESSIVE", "FULL_PAPER"]
RISK_MODE_NAMES: tuple[RiskModeName, ...] = ("SAFE", "AGGRESSIVE", "FULL_PAPER")


@dataclass(frozen=True)
class RiskModeProfile:
    name: RiskModeName
    description: str
    allowed_statuses: frozenset[str]
    min_sample_size: int
    require_medium_high_confidence: bool
    max_risk_per_trade: float
    max_wallet_exposure: float
    max_market_exposure: float
    max_total_exposure: float
    max_open_positions: int | None
    max_daily_trades: int | None
    require_copyable_edge: bool
    require_orderbook_quality: bool
    require_liquidity_check: bool
    require_spread_check: bool
    live_allowed: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def no_daily_trade_count_limit(self) -> bool:
        return self.max_daily_trades is None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "allowed_statuses": sorted(self.allowed_statuses),
            "min_sample_size": self.min_sample_size,
            "require_medium_high_confidence": self.require_medium_high_confidence,
            "max_risk_per_trade": self.max_risk_per_trade,
            "max_wallet_exposure": self.max_wallet_exposure,
            "max_market_exposure": self.max_market_exposure,
            "max_total_exposure": self.max_total_exposure,
            "max_open_positions": self.max_open_positions,
            "max_daily_trades": self.max_daily_trades,
            "no_daily_trade_count_limit": self.no_daily_trade_count_limit,
            "require_copyable_edge": self.require_copyable_edge,
            "require_orderbook_quality": self.require_orderbook_quality,
            "require_liquidity_check": self.require_liquidity_check,
            "require_spread_check": self.require_spread_check,
            "live_allowed": self.live_allowed,
            "notes": list(self.notes),
        }


SAFE = RiskModeProfile(
    name="SAFE",
    description=(
        "Only STRONG/ELITE wallets validated out-of-sample. Tight per-trade and total "
        "exposure caps. CANDIDATE_ELITE rejected unless the operator promotes manually."
    ),
    allowed_statuses=frozenset({"STRONG", "ELITE"}),
    min_sample_size=30,
    require_medium_high_confidence=True,
    max_risk_per_trade=0.01,
    max_wallet_exposure=0.05,
    max_market_exposure=0.03,
    max_total_exposure=0.15,
    max_open_positions=5,
    max_daily_trades=10,
    require_copyable_edge=True,
    require_orderbook_quality=True,
    require_liquidity_check=True,
    require_spread_check=True,
    live_allowed=False,
    notes=(
        "default mode at startup",
        "live execution is blocked at the compliance layer regardless of this flag",
    ),
)


AGGRESSIVE = RiskModeProfile(
    name="AGGRESSIVE",
    description=(
        "STRONG/ELITE plus CANDIDATE_ELITE wallets with sample ≥ 20 resolved markets. "
        "Wider per-trade / per-wallet caps. No daily trade-count cap, exposure caps still binding."
    ),
    allowed_statuses=frozenset({"STRONG", "ELITE", "CANDIDATE_ELITE"}),
    min_sample_size=20,
    require_medium_high_confidence=False,
    max_risk_per_trade=0.02,
    max_wallet_exposure=0.08,
    max_market_exposure=0.05,
    max_total_exposure=0.25,
    max_open_positions=15,
    max_daily_trades=None,
    require_copyable_edge=True,
    require_orderbook_quality=True,
    require_liquidity_check=True,
    require_spread_check=True,
    live_allowed=False,
)


FULL_PAPER = RiskModeProfile(
    name="FULL_PAPER",
    description=(
        "Mirror every signal from any validated wallet. No daily count cap, but per-trade, "
        "per-wallet, per-market and total exposure caps still apply, and the kill switch "
        "always wins. Live execution is permanently blocked by this profile."
    ),
    allowed_statuses=frozenset({"STRONG", "ELITE", "CANDIDATE_ELITE"}),
    min_sample_size=10,
    require_medium_high_confidence=False,
    max_risk_per_trade=0.01,
    max_wallet_exposure=0.10,
    max_market_exposure=0.07,
    max_total_exposure=0.40,
    max_open_positions=None,
    max_daily_trades=None,
    require_copyable_edge=True,
    require_orderbook_quality=True,
    require_liquidity_check=True,
    require_spread_check=True,
    live_allowed=False,
    notes=(
        "FULL MODE = paper only by default. High aggregate risk. No live execution.",
        "Built for backtest comparison vs SAFE / AGGRESSIVE.",
    ),
)


_REGISTRY: dict[RiskModeName, RiskModeProfile] = {
    "SAFE": SAFE,
    "AGGRESSIVE": AGGRESSIVE,
    "FULL_PAPER": FULL_PAPER,
}


def get_profile(name: str | None) -> RiskModeProfile:
    """Resolve a profile by name. Falls back to SAFE for unknown values so the
    bot never silently runs without a mode."""
    if not name:
        return SAFE
    upper = name.upper().strip()
    return _REGISTRY.get(upper, SAFE)  # type: ignore[arg-type]


def list_profiles() -> list[dict[str, object]]:
    return [_REGISTRY[name].to_dict() for name in RISK_MODE_NAMES]
