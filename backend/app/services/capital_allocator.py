"""CapitalAllocator — single trade-decision engine for paper + live.

v0.7.2: there are no named modes. The bot has ONE operational engine
(this allocator) and the operator only configures their CAPITAL. The
capital-aware sliding scale (`compute_dynamic_thresholds`) tightens the
gate when the bankroll is small, relaxes back to base when it's large.

Two presets ship in ``app/config/presets.yaml``:
``BASE_PAPER`` and ``BASE_LIVE``. Both feed the same engine; the only
difference is whether ``live_allowed`` is true and the EV-floor that
absorbs taker fees.

Reason codes the allocator may emit (16 total — ACCEPT plus 15 REJECT):

    ACCEPT
    KILL_SWITCH
    INSUFFICIENT_CAPITAL
    WALLET_TIER
    LOW_EV
    LOW_EDGE
    WIDE_SPREAD
    LOW_LIQUIDITY
    BAD_ORDERBOOK
    LATE_ENTRY
    NEGATIVE_EV_AFTER_FEES
    INSUFFICIENT_CAPITAL_FOR_STAKE
    MAX_TOTAL_EXPOSURE
    MAX_WALLET_EXPOSURE
    MAX_MARKET_EXPOSURE
    MAX_POSITIONS

The allocator is pure: no DB access, no side effects, no logging. Pass
in the signal + portfolio snapshot + params; get a decision back.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


REJECT_CODES: tuple[str, ...] = (
    "KILL_SWITCH",
    "LIVE_DISABLED",
    "MIN_ORDER_UNVERIFIED",
    "INSUFFICIENT_CAPITAL",
    "WALLET_TIER",
    "LOW_EV",
    "LOW_EDGE",
    "WIDE_SPREAD",
    "LOW_LIQUIDITY",
    "BAD_ORDERBOOK",
    "LATE_ENTRY",
    "NEGATIVE_EV_AFTER_FEES",
    "INSUFFICIENT_CAPITAL_FOR_STAKE",
    "MAX_TOTAL_EXPOSURE",
    "MAX_WALLET_EXPOSURE",
    "MAX_MARKET_EXPOSURE",
    "MAX_POSITIONS",
    "CAPITAL_LOCK_TOO_LONG",  # v0.7.6 B13
    "UNKNOWN_DEADLINE",        # v0.7.8 P4 — small-capital safety
    "UNKNOWN_CATEGORY",        # v0.7.8 P4 — small-capital safety
    "MIN_ORDER_EXCEEDS_MARKET_CAP",  # v0.7.8 dynamic-caps — min_stake > dynamic per-market cap remaining
    "MIN_ORDER_EXCEEDS_WALLET_CAP",  # v0.7.8 dynamic-caps — min_stake > dynamic per-wallet cap remaining
    "DATA_MISSING_MARKET_METADATA",  # v0.7.8 C1 — resolver fetched nothing (Gamma error or empty)
    "ORDERBOOK_UNAVAILABLE",         # v0.7.8 C1 — Gamma OK but CLOB orderbook missing/empty
    "MARKET_METADATA_FETCH_FAILED",  # v0.7.8 C1 — transient resolver failure (network, parse)
    "MARKET_METADATA_NOT_FOUND",     # v0.7.8 C1 — Gamma confirms market unknown (delisted/wrong)
)


# v0.7.8 P4 — small-capital threshold below which UNKNOWN deadline / category
# automatically rejects. Above this, the trade can still go through but
# routes to the conservative-fee fallback.
SMALL_CAPITAL_UNKNOWN_THRESHOLD_USD = 200.0


BAD_ORDERBOOK_VALUES: frozenset[str] = frozenset({"UNTRADABLE", "BAD", "INSUFFICIENT_DATA"})
BLOCKED_WALLET_TIERS: frozenset[str] = frozenset({"BLOCKED", "SUSPICIOUS", "IGNORE", "WEAK"})

CATEGORY_FEE_BUFFERS: dict[str, float] = {
    "politics": 0.04,
    "political": 0.04,
    "crypto": 0.072,
    "geopolitical": 0.0,
    "geopolitics": 0.0,
}
UNKNOWN_CATEGORY_FEE_BUFFER = 0.05
LIVE_BASE_EV_FLOOR = 0.05
SMALL_CAPITAL_LIVE_EV_FLOOR = 0.10


def _normalise_category(category: str | None) -> str:
    if not category:
        return "unknown"
    return category.strip().lower().replace("-", "_").replace(" ", "_")


def category_fee_buffer(category: str | None) -> tuple[float, str | None]:
    """Return the conservative one-fee live buffer for a market category."""
    normalised = _normalise_category(category)
    if normalised in {"unknown", "uncategorised", "uncategorized", "uncategorised_markets"}:
        return UNKNOWN_CATEGORY_FEE_BUFFER, "NEEDS_CATEGORY_MAPPING"
    if normalised in CATEGORY_FEE_BUFFERS:
        return CATEGORY_FEE_BUFFERS[normalised], None
    if "politic" in normalised:
        return CATEGORY_FEE_BUFFERS["politics"], None
    if "crypto" in normalised:
        return CATEGORY_FEE_BUFFERS["crypto"], None
    if "geopolitic" in normalised:
        return CATEGORY_FEE_BUFFERS["geopolitical"], None
    return UNKNOWN_CATEGORY_FEE_BUFFER, "NEEDS_CATEGORY_MAPPING"


@dataclass
class TradeSignal:
    """Snapshot of the single trade we're considering."""

    wallet_address: str
    wallet_tier: str
    market_id: str
    side: str
    price: float
    size: float
    notional_usd: float
    ev_lower_bound: float
    copyable_edge: float
    spread: float
    liquidity: float
    orderbook_quality: str = "OK"
    # v0.7.2 B1 fix — liquidity_score is the 0-100 normalised score from
    # tradeauditrecord.market_liquidity_score. The legacy `liquidity` field
    # was being interpreted as USD notional but the polling pipeline was
    # filling it with the score, leading to 100% LOW_LIQUIDITY rejects.
    # When liquidity_score is provided, the allocator uses it; otherwise
    # it falls back to the legacy USD comparison for backward compat.
    liquidity_score: float | None = None
    category: str | None = None
    copy_delay_seconds: float = 0.0
    price_deterioration: float = 0.0
    # v0.7.2 P1.1 — duration-aware sizing.
    # Hours until market resolution (None = unknown / no time-to-resolution data).
    expected_holding_time_hours: float | None = None


# Holding-time bucket thresholds (in hours).
HOLDING_BUCKET_SHORT_MAX_HOURS = 24.0    # SHORT: < 24h
HOLDING_BUCKET_MEDIUM_MAX_HOURS = 168.0  # MEDIUM: 1d–7d


def holding_time_bucket(hours: float | None) -> str:
    """Classify expected hold time into SHORT / MEDIUM / LONG / UNKNOWN.

    SHORT trades recycle capital fast — at small bankrolls the engine
    prefers them. The bucket is consumed by `compute_sizing` to apply a
    small multiplier bonus/penalty.
    """
    if hours is None or hours < 0:
        return "UNKNOWN"
    if hours < HOLDING_BUCKET_SHORT_MAX_HOURS:
        return "SHORT"
    if hours < HOLDING_BUCKET_MEDIUM_MAX_HOURS:
        return "MEDIUM"
    return "LONG"


# Sizing multiplier per bucket. Small capital prefers SHORT (capital
# returns to the pool sooner). LONG ties up capital for weeks → small
# penalty. UNKNOWN stays neutral.
HOLDING_BUCKET_MULTIPLIER: dict[str, float] = {
    "SHORT": 1.10,
    "MEDIUM": 1.00,
    "LONG": 0.85,
    "UNKNOWN": 1.00,
}


@dataclass
class PortfolioState:
    """Current portfolio at the moment we evaluate the signal."""

    capital_total: float
    capital_available: float
    kill_switch: bool = False
    exposure_total: float = 0.0
    exposure_per_wallet: dict[str, float] = field(default_factory=dict)
    exposure_per_market: dict[str, float] = field(default_factory=dict)
    open_positions_count: int = 0
    execution_mode: str = "PAPER"
    live_enabled: bool = False
    min_order_size_status: str = "unverified"
    # v0.7.8 P6 — R-based dynamic sizing multiplier (winning=2.0, losing=1.0).
    # Default 1.0 keeps existing tests/replay scripts unchanged (= 1R = base risk).
    # Production engines pass 2.0 (winning) / 1.0 (losing) from BotState.
    current_r_multiplier: float = 1.0


@dataclass
class AllocatorParams:
    """One preset's worth of allocator config — loaded from YAML."""

    name: str
    # Hard exposure (fractions of capital_total)
    max_total_exposure: float
    max_wallet_exposure: float
    max_market_exposure: float
    max_open_positions: int | None
    reserve_buffer: float
    # Sizing
    risk_per_trade: float
    min_stake: float
    # Quality gates
    allowed_tiers: frozenset[str]
    min_ev_lb: float
    min_copyable_edge: float
    max_spread_pct: float
    min_liquidity: float                       # legacy USD notional gate (kept for back-compat)
    # Fees model
    polymarket_fee_pct: float
    min_edge_after_fees: float
    # Late entry
    late_entry_thresholds: dict[str, float]
    late_entry_default: float
    # Misc
    require_orderbook_quality: bool = True
    live_allowed: bool = False
    description: str = ""
    hard_safety_caps: dict[str, float] = field(default_factory=dict)
    capital_aware_thresholds: bool = True
    # v0.7.2 B1 — preferred 0-100 score gate. None = use legacy USD gate.
    min_liquidity_score: float | None = None


@dataclass
class AllocatorDecision:
    accepted: bool
    reason_code: str | None
    sizing: float = 0.0
    reasoning: dict[str, Any] = field(default_factory=dict)
    exposure_after_trade: float = 0.0
    capital_after_trade: float = 0.0
    next_bottleneck: str | None = None

    @property
    def rejected(self) -> bool:
        return not self.accepted


# ---------------- Capital-aware graduated rules (v0.7.8 P6 final) ----------------
#
# Operator-aligned hybrid spec (per session 2026-05-04):
#
#   - Quality bar = STRICT toujours (vrais seuils ELITE/STRONG; cohorte sain).
#   - Tier capital adapts: wallet filter, max_open_positions, R-multipliers,
#     and (via duration_filter) duration allocation.
#
# Why graduated: at 100€ the priority is GROWTH (high cadence, accept STRONG
# variance, 2R bets). At 50k€+ the priority is PRESERVATION (ELITE only,
# fixed 1R, smaller per-trade risk). The bot must scale its risk posture
# with the bankroll.
#
# Tier table:
#
#   <  500 USD → SMALL    growth      ELITE+STRONG, 2R/1R, max_open 12
#   < 5000     → MEDIUM   balanced    ELITE+STRONG, 1.5R/0.75R, max_open 20
#   < 50000    → LARGE    preservation ELITE only, 1R/0.5R, max_open 40
#   ≥ 50000    → HUGE     institutional top-25% ELITE, 0.5R fixed, max_open 75
#
# r_winning_multiplier / r_losing_multiplier are NEW in P6 — they replace
# the previous hard-coded {2.0, 1.0} pair so the state machine adapts to
# the tier's risk posture. e.g. at LARGE the "winning" posture is 1R, not 2R.

# R-multiplier UNIFORM across all tiers (operator spec 2026-05-05):
# "on a un systeme qui fais tjr 2R si gagne et si perd 1R et reste un r
#  jusqu'à trade gagnant, si gagnant 2R et on y reste tant qu'on gagne"
# = always 2R when winning, 1R when losing (until a win), then back to 2R.
# Same logic at every capital tier — only max_pos and wallet filter graduate.
R_WINNING_MULTIPLIER: float = 2.0
R_LOSING_MULTIPLIER: float = 1.0


CAPITAL_TIER_RULES: list[dict[str, Any]] = [
    {
        # SMALL — Operator rule (2026-05-05): aucun STRONG, seulement ELITE.
        # Exposition limitée à 60% (capital protection).
        "name": "SMALL",
        "max_capital_exclusive": 500.0,
        "allowed_tiers_intersect": frozenset({"ELITE"}),
        "min_ev_lb_floor": None,
        "min_copyable_edge_floor": None,
        "max_open_positions_cap": 12,
        "risk_per_trade_cap": None,
        "max_total_exposure_cap": 0.60,
    },
    {
        # MEDIUM — STRONG s'intègre. Exposition montée à 70%.
        "name": "MEDIUM",
        "max_capital_exclusive": 5000.0,
        "allowed_tiers_intersect": frozenset({"ELITE", "STRONG"}),
        "min_ev_lb_floor": None,
        "min_copyable_edge_floor": None,
        "max_open_positions_cap": 20,
        "risk_per_trade_cap": None,
        "max_total_exposure_cap": 0.70,
    },
    {
        # LARGE — Diversification active. Exposition 80%.
        "name": "LARGE",
        "max_capital_exclusive": 50_000.0,
        "allowed_tiers_intersect": frozenset({"ELITE", "STRONG"}),
        "min_ev_lb_floor": None,
        "min_copyable_edge_floor": None,
        "max_open_positions_cap": 40,
        "risk_per_trade_cap": None,
        "max_total_exposure_cap": 0.80,
    },
    {
        # HUGE — Institutional. Exposition 90% (operator: "en huge c'est
        # vrm du 90 expo"). ELITE only car preservation domine.
        "name": "HUGE",
        "max_capital_exclusive": float("inf"),
        "allowed_tiers_intersect": frozenset({"ELITE"}),
        "min_ev_lb_floor": None,
        "min_copyable_edge_floor": None,
        "max_open_positions_cap": 75,
        "risk_per_trade_cap": None,
        "max_total_exposure_cap": 0.90,
    },
]


def _resolve_tier(capital_total: float) -> dict[str, Any]:
    for rule in CAPITAL_TIER_RULES:
        if capital_total < rule["max_capital_exclusive"]:
            return rule
    return CAPITAL_TIER_RULES[-1]


def get_r_multipliers_for_capital(capital_total: float) -> tuple[float, float]:
    """Return (r_winning, r_losing). v0.7.8 P6 LOCKED 2026-05-05 — operator
    spec is uniform across all tiers: 2R winning, 1R losing. State machine
    persisted in BotState.current_r_multiplier.

    Capital_total parameter kept for backward-compat (callers may pass it)
    but the multipliers don't depend on it anymore. Tiers control wallet
    filter + max_pos + duration; sizing posture is global."""
    return (R_WINNING_MULTIPLIER, R_LOSING_MULTIPLIER)


# ---------------- v0.7.8 — capital-aware dynamic exposure caps ----------------
#
# Static fractions like max_market_exposure=5% break at small capital: at
# $108, 5% = $5.40, but min_stake=$5 + quality boost (×1.5) + duration bonus
# (×1.10) produces $8.25, hard-rejected by MAX_MARKET_EXPOSURE. The bot is
# unusable at small capital.
#
# Dynamic caps fix this by:
#   1. Floor: cap_pct ≥ (min_stake / capital_total) × 1.20  — guarantees the
#      cap admits at least one min-stake order plus headroom for the quality
#      boost. If even the floor is too tight (capital < min_stake × 0.83),
#      reject cleanly with MIN_ORDER_EXCEEDS_*_CAP.
#   2. Capital-aware target: tighter at large capital (more diversification),
#      looser at small capital (must allow min_order).
#   3. Hard ceiling: never expose more than HARD_MAX_* on a single
#      market/wallet — no all-in déguisé.
#
# Tested at 50/75/100/150/250/500€ in test_capital_allocator_dynamic_caps.

HARD_MAX_MARKET_PCT: float = 0.20
HARD_MAX_WALLET_PCT: float = 0.30


def dynamic_market_cap_pct(
    capital_total_usd: float, min_stake_usd: float, base_pct: float
) -> float:
    """Capital-aware per-market exposure cap (fraction of capital_total).

    Returns ``base_pct`` at large capital (≥500 USD) and progressively
    looser caps at small capital, always ≥ ``(min_stake/capital) × 1.20``
    so the cap never blocks a min-order trade. Hard-bounded by
    ``HARD_MAX_MARKET_PCT`` so we never expose >20% on a single market."""
    capital = max(float(capital_total_usd), 1.0)
    floor = float(min_stake_usd) / capital * 1.20
    if capital <= 75.0:
        target = 0.15
    elif capital <= 150.0:
        target = 0.10
    elif capital <= 300.0:
        target = 0.07
    elif capital <= 500.0:
        target = 0.06
    else:
        target = float(base_pct)
    return min(HARD_MAX_MARKET_PCT, max(floor, target))


def dynamic_wallet_cap_pct(
    capital_total_usd: float, min_stake_usd: float, base_pct: float
) -> float:
    """Capital-aware per-wallet exposure cap (fraction of capital_total).

    Same shape as ``dynamic_market_cap_pct`` but with a more permissive
    table since one ELITE wallet is allowed to drive multiple positions.
    Hard-bounded by ``HARD_MAX_WALLET_PCT`` (30%) — never all-in on one
    wallet."""
    capital = max(float(capital_total_usd), 1.0)
    floor = float(min_stake_usd) / capital * 1.20
    if capital <= 75.0:
        target = 0.30
    elif capital <= 150.0:
        target = 0.20
    elif capital <= 300.0:
        target = 0.15
    elif capital <= 500.0:
        target = 0.12
    else:
        target = float(base_pct)
    return min(HARD_MAX_WALLET_PCT, max(floor, target))


def compute_dynamic_thresholds(
    capital_total: float, base_params: AllocatorParams
) -> AllocatorParams:
    """Return a new ``AllocatorParams`` with tightened thresholds for the
    given capital level. Pure function — never mutates ``base_params``.

    Honours the `capital_aware_thresholds` flag: if False, returns the
    base preset unchanged."""
    if not base_params.capital_aware_thresholds:
        return base_params
    if capital_total <= 0:
        # Treat zero/negative bankroll as ULTRA_SELECTIVE — caller will
        # also hit INSUFFICIENT_CAPITAL, but tighten anyway.
        capital_total = 0.0
    rule = _resolve_tier(capital_total)

    overrides: dict[str, Any] = {}

    # allowed_tiers: intersection (only tighten)
    if rule["allowed_tiers_intersect"] is not None:
        intersected = base_params.allowed_tiers & rule["allowed_tiers_intersect"]
        overrides["allowed_tiers"] = intersected
    # min_ev_lb: floor
    if rule["min_ev_lb_floor"] is not None:
        overrides["min_ev_lb"] = max(base_params.min_ev_lb, rule["min_ev_lb_floor"])
    # min_copyable_edge: floor
    if rule["min_copyable_edge_floor"] is not None:
        overrides["min_copyable_edge"] = max(
            base_params.min_copyable_edge, rule["min_copyable_edge_floor"]
        )
    # max_open_positions: cap (smaller wins, None = unlimited)
    if rule["max_open_positions_cap"] is not None:
        if base_params.max_open_positions is None:
            overrides["max_open_positions"] = rule["max_open_positions_cap"]
        else:
            overrides["max_open_positions"] = min(
                base_params.max_open_positions, rule["max_open_positions_cap"]
            )
    # risk_per_trade: cap (smaller wins)
    if rule["risk_per_trade_cap"] is not None:
        overrides["risk_per_trade"] = min(
            base_params.risk_per_trade, rule["risk_per_trade_cap"]
        )
    # v0.7.8 P6 LOCKED — max_total_exposure_cap graduated par tier
    # (60% SMALL, 70% MEDIUM, 80% LARGE, 90% HUGE).
    # Cap = take min(base, tier_cap) so we never loosen.
    _exposure_cap = rule.get("max_total_exposure_cap")
    if _exposure_cap is not None:
        overrides["max_total_exposure"] = min(
            base_params.max_total_exposure, _exposure_cap
        )

    # v0.7.8 dynamic-caps — capital-aware exposure caps. The static 5%/10%
    # fractions block min-order trades at small capital. Replace with
    # min-order-aware floors + capital-aware targets, hard-bounded.
    overrides["max_market_exposure"] = dynamic_market_cap_pct(
        capital_total, base_params.min_stake, base_params.max_market_exposure
    )
    overrides["max_wallet_exposure"] = dynamic_wallet_cap_pct(
        capital_total, base_params.min_stake, base_params.max_wallet_exposure
    )

    if not overrides:
        return base_params
    return replace(base_params, **overrides)


class CapitalAllocator:
    """Pure trade-decision engine.

    One call = one ACCEPT or one REJECT with a single reason code. All
    rules are evaluated in a deterministic order so the first failure
    wins (cheapest-first: hard stops, then quality, then market, then
    fees, then sizing/exposure)."""

    def evaluate_trade(
        self,
        signal: TradeSignal,
        state: PortfolioState,
        params: AllocatorParams,
    ) -> AllocatorDecision:
        # 0 — Capital-aware tightening (no-op if preset opts out or capital
        # is already in the FULL/PERMISSIVE tier).
        effective_params = compute_dynamic_thresholds(state.capital_total, params)

        return self._evaluate(signal, state, effective_params, base_params=params)

    def _evaluate(
        self,
        signal: TradeSignal,
        state: PortfolioState,
        params: AllocatorParams,
        *,
        base_params: AllocatorParams,
    ) -> AllocatorDecision:
        # 1 — Hard stops
        if state.kill_switch:
            return AllocatorDecision(
                False,
                "KILL_SWITCH",
                reasoning={"preset": base_params.name},
                next_bottleneck="kill_switch",
            )
        live_mode = state.execution_mode.upper() == "LIVE"
        if live_mode and (not state.live_enabled or not params.live_allowed):
            return AllocatorDecision(
                False,
                "LIVE_DISABLED",
                reasoning={
                    "execution_mode": state.execution_mode,
                    "live_enabled": state.live_enabled,
                    "preset_live_allowed": params.live_allowed,
                },
                next_bottleneck="live_enablement",
            )
        if live_mode and state.min_order_size_status.lower() != "verified":
            return AllocatorDecision(
                False,
                "MIN_ORDER_UNVERIFIED",
                reasoning={
                    "min_order_size": params.min_stake,
                    "min_order_size_status": state.min_order_size_status,
                },
                next_bottleneck="empirical_min_order_test",
            )
        if state.capital_available < params.min_stake:
            return AllocatorDecision(
                False,
                "INSUFFICIENT_CAPITAL",
                reasoning={
                    "capital_available": state.capital_available,
                    "min_stake": params.min_stake,
                },
                next_bottleneck="capital_available",
            )

        # 2 — Wallet / signal quality
        fee_buffer, category_warning = category_fee_buffer(signal.category)
        effective_min_ev = params.min_ev_lb
        if live_mode:
            effective_min_ev = max(effective_min_ev, LIVE_BASE_EV_FLOOR, fee_buffer)
            if state.capital_total <= 50:
                effective_min_ev = max(effective_min_ev, SMALL_CAPITAL_LIVE_EV_FLOOR)

        if signal.wallet_tier in BLOCKED_WALLET_TIERS or signal.wallet_tier not in params.allowed_tiers:
            return AllocatorDecision(
                False,
                "WALLET_TIER",
                reasoning={
                    "wallet_tier": signal.wallet_tier,
                    "allowed": sorted(params.allowed_tiers),
                },
                next_bottleneck="wallet_tier",
            )
        if signal.ev_lower_bound < effective_min_ev:
            return AllocatorDecision(
                False,
                "LOW_EV",
                reasoning={
                    "ev_lower_bound": signal.ev_lower_bound,
                    "min_ev_lb": params.min_ev_lb,
                    "effective_min_ev_lb": effective_min_ev,
                    "fee_buffer": fee_buffer,
                    "category": signal.category,
                    "category_warning": category_warning,
                    "small_capital_live_floor": SMALL_CAPITAL_LIVE_EV_FLOOR if live_mode and state.capital_total <= 50 else None,
                },
                next_bottleneck="fee_adjusted_ev" if live_mode else "ev_lower_bound",
            )
        if signal.copyable_edge < params.min_copyable_edge:
            return AllocatorDecision(
                False,
                "LOW_EDGE",
                reasoning={
                    "copyable_edge": signal.copyable_edge,
                    "min_copyable_edge": params.min_copyable_edge,
                },
                next_bottleneck="copyable_edge",
            )

        # 3 — Market conditions
        # v0.7.8 P6 B16-B17: ELITE wallets on crypto 5-min markets often
        # lack public CLOB orderbook data → spread/liquidity/edge scores
        # are 0 or unreliable. In paper mode, skip data-quality gates for
        # ELITE. These MUST be enforced for live trading.
        from app.config import get_settings as _get_settings
        _s = _get_settings()
        _elite_paper_bypass = (
            signal.wallet_tier == "ELITE"
            and _s.paper_trading_enabled
            and not _s.live_enabled
        )
        if signal.spread > params.max_spread_pct and not _elite_paper_bypass:
            return AllocatorDecision(
                False,
                "WIDE_SPREAD",
                reasoning={"spread": signal.spread, "max": params.max_spread_pct},
            )
        # v0.7.2 B1 fix — prefer the 0-100 liquidity_score gate when both
        # the signal and preset provide one. Falls back to the legacy USD
        # notional comparison for back-compat (tests / synthetic harnesses
        # that haven't migrated yet).
        if (
            signal.liquidity_score is not None
            and params.min_liquidity_score is not None
            and not _elite_paper_bypass
        ):
            if signal.liquidity_score < params.min_liquidity_score:
                return AllocatorDecision(
                    False,
                    "LOW_LIQUIDITY",
                    reasoning={
                        "liquidity_score": signal.liquidity_score,
                        "min_score": params.min_liquidity_score,
                        "gate": "score",
                    },
                )
        elif signal.liquidity < params.min_liquidity and not _elite_paper_bypass:
            return AllocatorDecision(
                False,
                "LOW_LIQUIDITY",
                reasoning={
                    "liquidity": signal.liquidity,
                    "min": params.min_liquidity,
                    "gate": "usd_notional_legacy",
                },
            )
        if (
            params.require_orderbook_quality
            and (signal.orderbook_quality or "").upper() in BAD_ORDERBOOK_VALUES
            and not _elite_paper_bypass
        ):
            return AllocatorDecision(
                False,
                "BAD_ORDERBOOK",
                reasoning={"orderbook_quality": signal.orderbook_quality},
            )

        # 4 — Late entry (per-category threshold)
        threshold = params.late_entry_thresholds.get(
            signal.category or "", params.late_entry_default
        )
        if signal.copy_delay_seconds > threshold:
            return AllocatorDecision(
                False,
                "LATE_ENTRY",
                reasoning={
                    "copy_delay_seconds": signal.copy_delay_seconds,
                    "threshold": threshold,
                    "category": signal.category,
                },
            )

        # 4-quater — v0.7.8 P4 small-capital safety: refuse Unknown
        # deadline / category. The user spec :
        #   "0 acceptation Unknown category/deadline at small capital"
        # The threshold is SMALL_CAPITAL_UNKNOWN_THRESHOLD_USD ($200 ≈ 185€).
        # Above this, the trade still goes through (we accept the
        # uncertainty); below, we hard-reject so the operator never
        # locks up small capital on data we don't trust.
        if state.capital_total < SMALL_CAPITAL_UNKNOWN_THRESHOLD_USD and not _elite_paper_bypass:
            cat_norm = (signal.category or "").strip().lower()
            # v0.7.8 P5-B — align with category_fee_buffer: any "unknown-class"
            # label (including the upstream default "uncategorised" when the
            # resolver returns no category) must be rejected at small capital.
            # Was previously only catching exact "unknown" and ""; the smoke
            # v5 stop confirmed this gap (3/4 ELITE trades opened on
            # category=Uncategorised markets).
            if not cat_norm or cat_norm in {"unknown", "uncategorised", "uncategorized", "uncategorised_markets"}:
                return AllocatorDecision(
                    False,
                    "UNKNOWN_CATEGORY",
                    reasoning={
                        "category": signal.category,
                        "capital_total": state.capital_total,
                        "policy": "small_capital_no_unknown_category",
                    },
                    next_bottleneck="market_metadata",
                )
            if signal.expected_holding_time_hours is None:
                return AllocatorDecision(
                    False,
                    "UNKNOWN_DEADLINE",
                    reasoning={
                        "expected_holding_time_hours": None,
                        "capital_total": state.capital_total,
                        "policy": "small_capital_no_unknown_deadline",
                    },
                    next_bottleneck="market_metadata",
                )

        # 4-bis — v0.7.6 B13 capital-aware duration filter.
        #
        # When ``expected_holding_time_hours`` is known and the operator's
        # capital is small, refuse long-locks unless the EV is exceptional.
        # This stops a $50-cohort from getting all its money locked in
        # 7-day VERY_LONG markets.
        from app.services.duration_filter import (
            classify_duration as _b13_classify,
            compute_capital_lock_penalty as _b13_lock_pen,
            compute_capital_turnover_score as _b13_turnover,
            compute_ev_per_hour as _b13_ev_per_h,
            filter_duration_for_capital as _b13_filter,
        )
        if signal.expected_holding_time_hours is not None and signal.expected_holding_time_hours > 0:
            exp_minutes = float(signal.expected_holding_time_hours) * 60.0
            duration_bucket = _b13_classify(exp_minutes)
            ev_per_hour = _b13_ev_per_h(signal.ev_lower_bound, exp_minutes)
            lock_pen = _b13_lock_pen(exp_minutes, state.capital_total)
            turnover_score = _b13_turnover(ev_per_hour, lock_pen)
            allowed_dur, dur_reason = _b13_filter(
                state.capital_total,
                duration_bucket,
                expected_minutes=exp_minutes,  # v0.7.8 P6 — pass minutes for 10min cutoff at small capital
                ev_lower_bound=signal.ev_lower_bound,
                capital_turnover_score=turnover_score,
            )
            if not allowed_dur:
                return AllocatorDecision(
                    False,
                    dur_reason or "CAPITAL_LOCK_TOO_LONG",
                    reasoning={
                        "expected_holding_time_hours": signal.expected_holding_time_hours,
                        "duration_bucket": duration_bucket,
                        "ev_per_hour": round(ev_per_hour, 6),
                        "capital_lock_penalty": round(lock_pen, 4),
                        "capital_turnover_score": round(turnover_score, 2),
                        "capital_total": state.capital_total,
                        "ev_lower_bound": signal.ev_lower_bound,
                    },
                    next_bottleneck="duration_filter",
                )

        # 5 — Fees check.
        #
        # v0.7.6 B12 — switch to the dynamic Polymarket fee formula
        # ``fee = C × rate × p × (1-p)`` whenever a category is known.
        # This replaces the flat ``polymarket_fee_pct`` heuristic for both
        # paper and live, so paper accurately reflects the cost the live
        # engine will see. Falls back to the legacy buffer if size or price
        # are missing (defensive).
        from app.services.fee_engine import (
            ev_after_fee as _ev_after_fee_b12,
            edge_after_dynamic_fee as _edge_after_b12,
            is_high_fee_zone as _is_high_fee_zone,
        )
        try:
            ev_after_fees_b12 = _ev_after_fee_b12(
                ev_lower_bound=signal.ev_lower_bound,
                category=signal.category,
                price=signal.price,
                size=signal.size,
            )
        except Exception:
            ev_after_fees_b12 = None

        if ev_after_fees_b12 is not None and signal.size > 0 and 0 < signal.price < 1:
            ev_after_fees = ev_after_fees_b12
            fee_source = "B12_dynamic"
            # B12-A — universal min EV after dynamic fee (paper + live).
            # Conservative 0.01 floor catches negative-edge trades the
            # legacy 4% flat would have missed.
            if ev_after_fees < 0.01:
                return AllocatorDecision(
                    False,
                    "NEGATIVE_EV_AFTER_FEES",
                    reasoning={
                        "ev_lower_bound": signal.ev_lower_bound,
                        "ev_after_fees_estimate": ev_after_fees,
                        "fee_source": fee_source,
                        "category": signal.category,
                        "price": signal.price,
                        "min_floor": 0.01,
                    },
                    next_bottleneck="fee_adjusted_ev",
                )
            # B12-B — penalize trades sitting in the high-fee zone
            # (p ∈ [0.45, 0.55]) by 0.5pp on the edge measure.
            if _is_high_fee_zone(signal.price):
                signal_edge_after_zone = _edge_after_b12(
                    copyable_edge=signal.copyable_edge,
                    category=signal.category,
                    price=signal.price,
                )
                # If the post-zone edge falls below the preset floor, reject.
                if signal_edge_after_zone < params.min_copyable_edge:
                    return AllocatorDecision(
                        False,
                        "LOW_EDGE",
                        reasoning={
                            "copyable_edge": signal.copyable_edge,
                            "edge_after_high_fee_zone_penalty": signal_edge_after_zone,
                            "min_copyable_edge": params.min_copyable_edge,
                            "fee_zone": "HIGH",
                        },
                        next_bottleneck="copyable_edge",
                    )
        else:
            # Legacy path when category/price/size unavailable.
            ev_after_fees = signal.ev_lower_bound - (fee_buffer if live_mode else params.polymarket_fee_pct)
            fee_source = "legacy_flat"
            if live_mode and ev_after_fees < params.min_edge_after_fees:
                return AllocatorDecision(
                    False,
                    "NEGATIVE_EV_AFTER_FEES",
                    reasoning={
                        "ev_lower_bound": signal.ev_lower_bound,
                        "ev_after_fees_estimate": ev_after_fees,
                        "min_edge_after_fees": params.min_edge_after_fees,
                        "fee_buffer": fee_buffer,
                        "fee_source": fee_source,
                        "category": signal.category,
                        "category_warning": category_warning,
                    },
                    next_bottleneck="fee_adjusted_ev",
                )

        # 6 — Sizing
        sizing = self.compute_sizing(signal, state, params)
        if sizing < params.min_stake:
            # v0.7.8 dynamic-caps — distinguish "cap too tight for min order"
            # from "actually no capital left". Both block the trade, but
            # MIN_ORDER_EXCEEDS_*_CAP tells the operator the issue is
            # structural (cap configuration vs min order), not a depleted
            # bankroll. Order: market-cap first since it's the typical
            # constraint at small capital where many wallets converge on
            # one hot market.
            wallet_exp = state.exposure_per_wallet.get(signal.wallet_address, 0.0)
            market_exp = state.exposure_per_market.get(signal.market_id, 0.0)
            market_remaining = max(
                0.0, params.max_market_exposure * state.capital_total - market_exp
            )
            wallet_remaining = max(
                0.0, params.max_wallet_exposure * state.capital_total - wallet_exp
            )
            if market_remaining < params.min_stake:
                return AllocatorDecision(
                    False,
                    "MIN_ORDER_EXCEEDS_MARKET_CAP",
                    reasoning={
                        "min_stake": params.min_stake,
                        "market_cap_remaining": market_remaining,
                        "market_cap_pct": params.max_market_exposure,
                        "capital_total": state.capital_total,
                    },
                )
            if wallet_remaining < params.min_stake:
                return AllocatorDecision(
                    False,
                    "MIN_ORDER_EXCEEDS_WALLET_CAP",
                    reasoning={
                        "min_stake": params.min_stake,
                        "wallet_cap_remaining": wallet_remaining,
                        "wallet_cap_pct": params.max_wallet_exposure,
                        "capital_total": state.capital_total,
                    },
                )
            return AllocatorDecision(
                False,
                "INSUFFICIENT_CAPITAL_FOR_STAKE",
                reasoning={"sizing": sizing, "min_stake": params.min_stake},
            )
        if sizing > state.capital_available:
            return AllocatorDecision(
                False,
                "INSUFFICIENT_CAPITAL_FOR_STAKE",
                reasoning={
                    "sizing": sizing,
                    "capital_available": state.capital_available,
                },
            )

        # 7 — Exposure constraints (per fraction of capital_total)
        max_total_usd = params.max_total_exposure * state.capital_total
        if state.exposure_total + sizing > max_total_usd:
            return AllocatorDecision(
                False,
                "MAX_TOTAL_EXPOSURE",
                reasoning={
                    "current": state.exposure_total,
                    "with_new": state.exposure_total + sizing,
                    "cap_usd": max_total_usd,
                },
            )

        wallet_exp = state.exposure_per_wallet.get(signal.wallet_address, 0.0)
        max_wallet_usd = params.max_wallet_exposure * state.capital_total
        if wallet_exp + sizing > max_wallet_usd:
            return AllocatorDecision(
                False,
                "MAX_WALLET_EXPOSURE",
                reasoning={
                    "wallet": signal.wallet_address,
                    "current": wallet_exp,
                    "with_new": wallet_exp + sizing,
                    "cap_usd": max_wallet_usd,
                },
            )

        market_exp = state.exposure_per_market.get(signal.market_id, 0.0)
        max_market_usd = params.max_market_exposure * state.capital_total
        if market_exp + sizing > max_market_usd:
            return AllocatorDecision(
                False,
                "MAX_MARKET_EXPOSURE",
                reasoning={
                    "market": signal.market_id,
                    "current": market_exp,
                    "with_new": market_exp + sizing,
                    "cap_usd": max_market_usd,
                },
            )

        # 8 — Position count cap (only if user set one)
        if (
            params.max_open_positions is not None
            and state.open_positions_count >= params.max_open_positions
        ):
            return AllocatorDecision(
                False,
                "MAX_POSITIONS",
                reasoning={
                    "open_positions_count": state.open_positions_count,
                    "cap": params.max_open_positions,
                },
            )

        # ACCEPT
        return AllocatorDecision(
            True,
            None,
            sizing=sizing,
            reasoning={
                "preset": base_params.name,
                "effective_min_ev_lb": params.min_ev_lb,
                "effective_min_copyable_edge": params.min_copyable_edge,
                "effective_allowed_tiers": sorted(params.allowed_tiers),
                "effective_max_open_positions": params.max_open_positions,
                "ev_after_fees_estimate": ev_after_fees,
                "fee_buffer": fee_buffer,
                "category": signal.category,
                "category_warning": category_warning,
                "execution_mode": state.execution_mode,
                # v0.7.2 P1.1 — duration-aware metadata
                "holding_time_hours": signal.expected_holding_time_hours,
                "holding_time_bucket": holding_time_bucket(
                    signal.expected_holding_time_hours
                ),
            },
            exposure_after_trade=state.exposure_total + sizing,
            capital_after_trade=state.capital_available - sizing,
        )

    @staticmethod
    def compute_sizing(
        signal: TradeSignal, state: PortfolioState, params: AllocatorParams
    ) -> float:
        """Stake sizing per user spec, plus v0.7.2 P1.1 duration-aware bonus,
        v0.7.8 dynamic-cap clamp, and v0.7.8 P6 R-based dynamic multiplier.

        base = capital_total × risk_per_trade × current_r_multiplier
          where current_r_multiplier = 2.0 (winning) or 1.0 (losing).
          → at $100 capital, 2% risk_per_trade, winning: base = $4 (= 2R)
          → at $100 capital, 2% risk_per_trade, losing:  base = $2 (= 1R)
        clamp to >= min_stake
        clamp to <= 50% of available capital (avoid all-in)
        quality_multiplier = clamp(0.5 + ev_lb × 5, 0.5, 1.5)
        duration_multiplier (SHORT 1.10, MEDIUM 1.00, LONG 0.85, UNKNOWN 1.00)
        sized = base × quality_multiplier × duration_multiplier

        v0.7.8 dynamic-caps — clamp final sized to per-wallet/per-market cap
        REMAINING (cap_usd minus already-open exposure). If the clamp drops
        sized below min_stake, return the clamped value anyway (caller
        decides whether to reject MIN_ORDER_EXCEEDS_*_CAP or not). This
        prevents the bug where compute_sizing produced $8.25 against a
        $5.40 cap and the trade was rejected MAX_MARKET_EXPOSURE despite
        a perfectly fine $5.40 size being available.
        """
        # v0.7.8 P6 — R-based dynamic multiplier applied BEFORE min_stake floor.
        # On a losing streak, base shrinks to 1R = capital × risk_per_trade × 1.
        # On a winning streak, base = 2R = capital × risk_per_trade × 2.
        r_multiplier = max(0.0, float(state.current_r_multiplier or 1.0))
        base = state.capital_total * params.risk_per_trade * r_multiplier
        base = max(base, params.min_stake)
        base = min(base, state.capital_available * 0.5)
        ev_lb = max(0.0, signal.ev_lower_bound)
        quality_multiplier = max(0.5, min(0.5 + ev_lb * 5, 1.5))
        bucket = holding_time_bucket(signal.expected_holding_time_hours)
        duration_multiplier = HOLDING_BUCKET_MULTIPLIER.get(bucket, 1.0)
        sized = max(params.min_stake, base * quality_multiplier * duration_multiplier)

        # v0.7.8 — clamp to per-wallet / per-market cap remaining so the
        # downstream MAX_*_EXPOSURE gates don't false-reject a sized trade
        # that could simply have been smaller.
        wallet_exp = state.exposure_per_wallet.get(signal.wallet_address, 0.0)
        market_exp = state.exposure_per_market.get(signal.market_id, 0.0)
        wallet_remaining = max(
            0.0, params.max_wallet_exposure * state.capital_total - wallet_exp
        )
        market_remaining = max(
            0.0, params.max_market_exposure * state.capital_total - market_exp
        )
        sized = min(sized, wallet_remaining, market_remaining)

        return round(min(sized, state.capital_available), 4)
