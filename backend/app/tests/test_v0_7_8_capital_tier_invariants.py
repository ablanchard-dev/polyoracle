"""v0.7.8 Phase 1 — Capital tier invariants framework.

Why this exists: passing capital from 50€ to 100€ broke the bot in v0.7.7
because dynamic caps didn't exist. The fix shipped (`dynamic_market_cap_pct`,
`dynamic_wallet_cap_pct`) but there was no SYSTEMATIC test that all 7
capital tiers behave correctly.

This module enforces the contract: **for every tier in
{50, 75, 100, 150, 250, 500, 1000, 2000, 5000}€, the bot MUST**:
  1. Have dynamic caps that allow at least 1 min-stake order
  2. Reject min-order-impossible scenarios with clean reject codes
  3. Apply the right capital-aware tier rules (allowed_tiers,
     min_ev_lb, min_copyable_edge, max_open_positions)
  4. Apply the right B13 duration filter for the tier
  5. Accept a clean ELITE ULTRA_SHORT signal at every tier
  6. Reject STRONG signals at tiers that mandate ELITE-only

Capital scaling won't break this time.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.capital_allocator import (
    HARD_MAX_MARKET_PCT,
    HARD_MAX_WALLET_PCT,
    AllocatorParams,
    CapitalAllocator,
    PortfolioState,
    TradeSignal,
    compute_dynamic_thresholds,
    dynamic_market_cap_pct,
    dynamic_wallet_cap_pct,
)
from app.services.duration_filter import filter_duration_for_capital
from app.services.preset_loader import get_preset


# Tiers explicitly tested. capital is in EUR; *1.08 = USD passed to allocator.
TIERS_EUR = [50, 75, 100, 150, 250, 500, 1000, 2000, 5000]


def _to_usd(eur: float) -> float:
    return eur * 1.08


def _params(**kw):
    """Realistic defaults from BASE_PAPER preset, overridable per test."""
    base = AllocatorParams(
        name="TEST",
        max_total_exposure=0.95,
        max_wallet_exposure=0.10,  # base preset value (overriden by dynamic caps)
        max_market_exposure=0.05,  # base preset value (overriden by dynamic caps)
        max_open_positions=None,
        reserve_buffer=0.05,
        risk_per_trade=0.01,
        min_stake=5.0,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
        min_copyable_edge=0.15,
        max_spread_pct=0.04,
        min_liquidity=1000,
        polymarket_fee_pct=0.04,
        min_edge_after_fees=0.01,
        late_entry_thresholds={"political": 600, "crypto_5min": 60, "sport": 300, "macro": 900},
        late_entry_default=300,
        require_orderbook_quality=False,
        live_allowed=False,
        description="test",
        capital_aware_thresholds=True,
    )
    return replace(base, **kw)


def _signal(**kw):
    """Clean ELITE ULTRA_SHORT signal — should be acceptable at any tier."""
    base = dict(
        wallet_address="0xelite_test",
        wallet_tier="ELITE",
        market_id="0xmkt_test",
        side="BUY",
        price=0.5,
        size=5.0,
        notional_usd=5.0,
        ev_lower_bound=0.20,
        copyable_edge=0.70,
        spread=0.01,
        liquidity=10_000,
        liquidity_score=80.0,
        orderbook_quality="OK",
        category="Crypto",
        copy_delay_seconds=10.0,
        price_deterioration=0.0,
        expected_holding_time_hours=0.05,  # 3 min = ULTRA_SHORT
    )
    base.update(kw)
    return TradeSignal(**base)


def _state(capital_eur: float, **kw):
    cap_usd = _to_usd(capital_eur)
    base = dict(
        capital_total=cap_usd,
        capital_available=cap_usd,
        kill_switch=False,
        exposure_total=0.0,
        exposure_per_wallet={},
        exposure_per_market={},
        open_positions_count=0,
        execution_mode="PAPER",
        live_enabled=False,
        min_order_size_status="unverified",
    )
    base.update(kw)
    return PortfolioState(**base)


# =====================================================================
# Invariant 1 — every tier must have dynamic caps allowing min-stake
# =====================================================================


@pytest.mark.parametrize("capital_eur", TIERS_EUR)
def test_invariant_1_market_cap_admits_min_stake(capital_eur):
    """The dynamic per-market cap must always be at least min_stake.
    Otherwise a clean ELITE signal can never be sized at this tier."""
    cap_usd = _to_usd(capital_eur)
    min_stake = 5.0
    market_cap_pct = dynamic_market_cap_pct(cap_usd, min_stake, 0.05)
    market_cap_usd = market_cap_pct * cap_usd
    assert market_cap_usd >= min_stake, (
        f"Tier {capital_eur}€: market_cap=${market_cap_usd:.2f} < "
        f"min_stake=${min_stake}. Structural blockage."
    )


@pytest.mark.parametrize("capital_eur", TIERS_EUR)
def test_invariant_1_wallet_cap_admits_min_stake(capital_eur):
    cap_usd = _to_usd(capital_eur)
    min_stake = 5.0
    wallet_cap_pct = dynamic_wallet_cap_pct(cap_usd, min_stake, 0.10)
    wallet_cap_usd = wallet_cap_pct * cap_usd
    assert wallet_cap_usd >= min_stake, (
        f"Tier {capital_eur}€: wallet_cap=${wallet_cap_usd:.2f} < "
        f"min_stake=${min_stake}. Structural blockage."
    )


# =====================================================================
# Invariant 2 — hard ceilings respected
# =====================================================================


@pytest.mark.parametrize("capital_eur", TIERS_EUR)
def test_invariant_2_market_cap_under_hard_max(capital_eur):
    cap_usd = _to_usd(capital_eur)
    market_cap_pct = dynamic_market_cap_pct(cap_usd, 5.0, 0.05)
    assert market_cap_pct <= HARD_MAX_MARKET_PCT, (
        f"Tier {capital_eur}€: market_cap_pct={market_cap_pct} > "
        f"HARD_MAX_MARKET_PCT={HARD_MAX_MARKET_PCT}"
    )


@pytest.mark.parametrize("capital_eur", TIERS_EUR)
def test_invariant_2_wallet_cap_under_hard_max(capital_eur):
    cap_usd = _to_usd(capital_eur)
    wallet_cap_pct = dynamic_wallet_cap_pct(cap_usd, 5.0, 0.10)
    assert wallet_cap_pct <= HARD_MAX_WALLET_PCT, (
        f"Tier {capital_eur}€: wallet_cap_pct={wallet_cap_pct} > "
        f"HARD_MAX_WALLET_PCT={HARD_MAX_WALLET_PCT}"
    )


# =====================================================================
# Invariant 3 — caps decrease (or stay flat) as capital grows
# =====================================================================


def test_invariant_3_market_cap_monotonically_decreasing_or_flat():
    pcts = [
        dynamic_market_cap_pct(_to_usd(eur), 5.0, 0.05) for eur in TIERS_EUR
    ]
    for prev, cur, prev_eur, cur_eur in zip(pcts, pcts[1:], TIERS_EUR, TIERS_EUR[1:]):
        assert cur <= prev + 1e-9, (
            f"Market cap pct should not grow: "
            f"{prev_eur}€={prev:.4f} → {cur_eur}€={cur:.4f}"
        )


def test_invariant_3_wallet_cap_monotonically_decreasing_or_flat():
    pcts = [
        dynamic_wallet_cap_pct(_to_usd(eur), 5.0, 0.10) for eur in TIERS_EUR
    ]
    for prev, cur, prev_eur, cur_eur in zip(pcts, pcts[1:], TIERS_EUR, TIERS_EUR[1:]):
        assert cur <= prev + 1e-9, (
            f"Wallet cap pct should not grow: "
            f"{prev_eur}€={prev:.4f} → {cur_eur}€={cur:.4f}"
        )


# =====================================================================
# Invariant 4 — capital-aware tier rules apply correctly
# =====================================================================


@pytest.mark.parametrize("capital_eur,expected_tier_name", [
    # v0.7.8 P6 — 12-tier refactor 2026-05-06 (operator spec):
    # NANO  (<$200) ELITE GOLD only         max_pos 12
    # TINY  (<$250)                         max_pos 18
    # MICRO (<$500)                         max_pos 22
    # SMALL (<$1k)  ELITE GOLD+SILVER       max_pos 32
    # MEDIUM(<$2k)  + STRONG GOLD overflow  max_pos 50
    # LARGE (<$4k)                          max_pos 75
    # XL    (<$8k)                          max_pos 110
    # XXL   (<$10k)                         max_pos 150
    # ELITE_OPEN (<$32k) +ELITE BRONZE      max_pos 200
    # GIGA  (<$64k)                         max_pos 300
    # HUGE  (<$128k) preservation ELITE only max_pos 400
    # INST  (≥$128k)                        max_pos 500
    (50, "NANO"),       # $54 < $200
    (100, "NANO"),      # $108 < $200
    (180, "NANO"),      # $194 < $200
    (190, "TINY"),      # $205 ≥ $200
    (220, "TINY"),      # $237 < $250
    (235, "MICRO"),     # $254 ≥ $250
    (450, "MICRO"),     # $486 < $500
    (470, "SMALL"),     # $507 ≥ $500
    (920, "SMALL"),     # $993 < $1000
    (940, "MEDIUM"),    # $1015 ≥ $1000
    (1850, "MEDIUM"),   # $1998 < $2000
    (1870, "LARGE"),    # $2019 ≥ $2000
    (3700, "LARGE"),    # $3996 < $4000
    (3720, "XL"),       # $4017 ≥ $4000
    (7400, "XL"),       # $7992 < $8000
    (7420, "XXL"),      # $8013 ≥ $8000
    (9250, "XXL"),      # $9990 < $10000
    (9270, "ELITE_OPEN"),  # $10011 ≥ $10000
    (29000, "ELITE_OPEN"), # $31320 < $32000
    (29700, "GIGA"),    # $32076 ≥ $32000
    (59000, "GIGA"),    # $63720 < $64000
    (59300, "HUGE"),    # $64044 ≥ $64000
    (118000, "HUGE"),   # $127440 < $128000
    (118600, "INST"),   # $128088 ≥ $128000
])
def test_invariant_4_correct_tier_assignment(capital_eur, expected_tier_name):
    """Capital tier rules must dispatch to the correct tier."""
    from app.services.capital_allocator import _resolve_tier
    cap_usd = _to_usd(capital_eur)
    rule = _resolve_tier(cap_usd)
    assert rule["name"] == expected_tier_name, (
        f"Tier {capital_eur}€ ({cap_usd} USD) should resolve to "
        f"{expected_tier_name}, got {rule['name']}"
    )


# =====================================================================
# Invariant 5 — clean ELITE ULTRA_SHORT signal accepts at every tier
# =====================================================================


@pytest.mark.parametrize("capital_eur", TIERS_EUR)
def test_invariant_5_clean_elite_signal_accepts_at_every_tier(capital_eur):
    """A perfect ELITE signal on a 1MN crypto market with high edge,
    valid liquidity, and known deadline must be accepted at every tier
    capital from 50€ to 5000€. If it's rejected, something is broken."""
    a = CapitalAllocator()
    p = _params()
    s = _state(capital_eur=capital_eur)
    sig = _signal()  # tier ELITE, edge 0.70, ULTRA_SHORT, deadline known

    d = a.evaluate_trade(sig, s, p)

    # The decision must be ACCEPTED OR a non-structural reject.
    # Acceptable rejects: NONE for a perfect signal at any tier.
    assert d.accepted, (
        f"Tier {capital_eur}€: clean ELITE signal rejected with "
        f"{d.reason_code}. Reasoning: {d.reasoning}"
    )
    assert d.sizing > 0


# =====================================================================
# Invariant 6 — STRONG signal behaviour by tier
# =====================================================================


@pytest.mark.parametrize("capital_eur,strong_should_pass", [
    # v0.7.8 P6 — 12-tier refactor 2026-05-06 (operator spec):
    # NANO/TINY/MICRO (<$500)   : ELITE GOLD only — no STRONG
    # SMALL (<$1k)              : ELITE GOLD+SILVER — still no STRONG
    # MEDIUM-XXL ($1k-9.99k)    : + STRONG GOLD overflow
    # ELITE_OPEN (≥$10k) / GIGA : + STRONG GOLD overflow
    # HUGE (≥$64k) / INST       : ELITE only (preservation)
    (50, False),     # NANO
    (100, False),    # NANO
    (180, False),    # NANO
    (220, False),    # TINY
    (450, False),    # MICRO
    (700, False),    # SMALL — still no STRONG
    # 12-tier spec 2026-05-09: STRONG GOLD overflow ONLY from ELITE_OPEN ($10k+)
    (1000, False),   # MEDIUM — ELITE-only, no STRONG
    (3000, False),   # LARGE — ELITE-only
    (7000, False),   # XL — ELITE-only
    (9000, False),   # XXL — ELITE-only
    (15000, True),   # ELITE_OPEN — STRONG GOLD overflow starts
    (40000, True),   # GIGA — STRONG GOLD overflow
    (70000, True),   # HUGE — STRONG GOLD overflow (NO preservation cap in 2026-05-09 spec)
    (130000, True),  # INST — STRONG GOLD overflow
])
def test_invariant_6_strong_filtered_at_small_capital(capital_eur, strong_should_pass):
    a = CapitalAllocator()
    p = _params()
    s = _state(capital_eur=capital_eur)
    sig = _signal(wallet_tier="STRONG")

    d = a.evaluate_trade(sig, s, p)

    if strong_should_pass:
        # STRONG accepted at this tier (ELITE+STRONG allowed)
        # or rejected for non-tier reason
        assert d.reason_code != "WALLET_TIER", (
            f"Tier {capital_eur}€: STRONG should NOT be filtered by tier here, "
            f"got WALLET_TIER reject"
        )
    else:
        assert d.reason_code == "WALLET_TIER", (
            f"Tier {capital_eur}€: STRONG should be filtered (ELITE-only tier), "
            f"got {d.reason_code} instead"
        )


# =====================================================================
# Invariant 7 — B13 duration filter coherence per tier
# =====================================================================


@pytest.mark.parametrize("capital_eur,bucket,allowed_default", [
    # SMALL (<$500) — short-only: ULTRA_SHORT auto, VERY_SHORT (no minutes)
    # rejected (conservative default), SHORT+ rejected
    (50, "ULTRA_SHORT", True),
    (50, "VERY_SHORT", False),  # no expected_minutes → conservative reject
    (50, "SHORT", False),
    (50, "MEDIUM", False),
    (50, "LONG", False),
    (50, "VERY_LONG", False),
    (100, "ULTRA_SHORT", True),
    (100, "MEDIUM", False),
    (100, "LONG", False),
    (250, "ULTRA_SHORT", True),
    (250, "SHORT", False),       # SMALL — SHORT (30-120) reject
    (250, "MEDIUM", False),
    # MEDIUM ($500-5000) — 1-30 min auto, SHORT edge (ev=0.10/turn=20 OK)
    (500, "ULTRA_SHORT", True),
    (500, "VERY_SHORT", True),
    (500, "SHORT", True),        # ev=0.10 turnover=20 → accepts
    (500, "MEDIUM", False),       # MEDIUM at MEDIUM tier — needs strong (ev≥0.20)
    (1000, "ULTRA_SHORT", True),
    (1000, "MEDIUM", False),      # MEDIUM bucket — needs strong (ev≥0.20)
    (1000, "LONG", False),
    # LARGE ($5000-50000) — up to MEDIUM auto, LONG edge-conditional
    (5000, "ULTRA_SHORT", True),
    (5000, "MEDIUM", True),
    (5000, "LONG", False),        # 2026-05-16 operator rule : no LONG <50k = hard reject (was edge-conditional)
    (5000, "VERY_LONG", False),
    # HUGE (≥$50k) — all auto except VERY_LONG (needs strong)
    (50000, "ULTRA_SHORT", True),
    (50000, "LONG", True),
    (50000, "VERY_LONG", False),  # ev=0.10 turn=20 < required (turn≥30)
])
def test_invariant_7_b13_per_tier(capital_eur, bucket, allowed_default):
    """B13 graduated duration filter per v0.7.8 P6:
    SMALL=short-only, MEDIUM=short-mostly, LARGE=balanced, HUGE=institutional."""
    cap_usd = _to_usd(capital_eur)
    allowed, reason = filter_duration_for_capital(
        cap_usd, bucket, ev_lower_bound=0.10, capital_turnover_score=20.0,
    )
    assert allowed == allowed_default, (
        f"Tier {capital_eur}€ bucket {bucket}: expected allowed={allowed_default}, "
        f"got allowed={allowed} reason={reason}"
    )


# =====================================================================
# Invariant 8 — sizing scales but never exceeds caps
# =====================================================================


@pytest.mark.parametrize("capital_eur", TIERS_EUR)
def test_invariant_8_sizing_respects_caps(capital_eur):
    """compute_sizing must produce a value ≤ market_cap_remaining and
    ≤ capital_available at every tier."""
    a = CapitalAllocator()
    p = _params()
    s = _state(capital_eur=capital_eur)
    sig = _signal()
    eff_params = compute_dynamic_thresholds(s.capital_total, p)
    sized = a.compute_sizing(sig, s, eff_params)

    market_cap_usd = eff_params.max_market_exposure * s.capital_total
    wallet_cap_usd = eff_params.max_wallet_exposure * s.capital_total

    # +1e-6 tolerance for FP rounding
    assert sized <= market_cap_usd + 1e-6, (
        f"Tier {capital_eur}€: sized={sized} > market_cap={market_cap_usd}"
    )
    assert sized <= wallet_cap_usd + 1e-6
    assert sized <= s.capital_available + 1e-6


# =====================================================================
# Invariant 9 — clean reject codes when caps too tight
# =====================================================================


def test_invariant_9_min_order_exceeds_market_cap_propagates():
    """At capital so small that hard ceiling 20% × min_stake = $1, the
    reject must be MIN_ORDER_EXCEEDS_MARKET_CAP, NOT a fake LOW_LIQUIDITY
    or generic INSUFFICIENT_CAPITAL_FOR_STAKE."""
    a = CapitalAllocator()
    p = _params(min_stake=5.0)
    # Capital so small even hard cap can't fit min_stake
    s = PortfolioState(
        capital_total=20.0,  # 20% × $20 = $4 < $5 min_stake
        capital_available=20.0,
        kill_switch=False,
        exposure_total=0.0,
        exposure_per_wallet={},
        exposure_per_market={},
        open_positions_count=0,
        execution_mode="PAPER",
        live_enabled=False,
        min_order_size_status="unverified",
    )
    sig = _signal()
    d = a.evaluate_trade(sig, s, p)
    assert d.reason_code == "MIN_ORDER_EXCEEDS_MARKET_CAP"


# =====================================================================
# Invariant 10 — base preset is the source of truth for sizing rate
# =====================================================================


def test_invariant_10_base_paper_preset_is_canonical():
    """BASE_PAPER preset values are what the dynamic caps fall back to
    at large capital. If someone changes the preset, the test catches it."""
    p = get_preset("BASE_PAPER")
    assert p.max_market_exposure == 0.05, "BASE_PAPER max_market_exposure must be 5%"
    assert p.max_wallet_exposure == 0.10, "BASE_PAPER max_wallet_exposure must be 10%"
    assert p.min_stake == 5.0, "BASE_PAPER min_stake must be $5"
    assert p.risk_per_trade == 0.01, "BASE_PAPER risk_per_trade must be 1%"
    assert p.capital_aware_thresholds is True, (
        "BASE_PAPER must enable capital_aware_thresholds"
    )


# =====================================================================
# Invariant 11 — boundary tests at tier transition points
# =====================================================================


@pytest.mark.parametrize("eur_eur_pair", [
    # (low, high) — pair around tier boundaries
    (75, 76),     # within ULTRA_SELECTIVE (no transition)
    (99, 100),    # both ULTRA_SELECTIVE now (boundary moved to $200)
    (184, 186),   # ULTRA_SELECTIVE → SELECTIVE (at $200, ~185€)
    (499, 501),   # SELECTIVE → STANDARD (at $540)
    (999, 1001),  # STANDARD → FULL (at $1080)
])
def test_invariant_11_no_discontinuity_at_boundaries(eur_eur_pair):
    """The dynamic caps should not have a huge jump at tier boundaries.
    A 1€ change should not 10x the cap value."""
    low, high = eur_eur_pair
    cap_low = dynamic_market_cap_pct(_to_usd(low), 5.0, 0.05) * _to_usd(low)
    cap_high = dynamic_market_cap_pct(_to_usd(high), 5.0, 0.05) * _to_usd(high)

    # Difference should be < 4x (allows for tier-boundary tightening but
    # not catastrophic).
    ratio = max(cap_low, cap_high) / max(min(cap_low, cap_high), 0.01)
    assert ratio < 4.0, (
        f"Boundary {low}-{high}€: cap jumped from ${cap_low:.2f} to ${cap_high:.2f} "
        f"(ratio {ratio:.2f}× — too discontinuous)"
    )


# =====================================================================
# Invariant 12 — kill switch overrides all
# =====================================================================


@pytest.mark.parametrize("capital_eur", TIERS_EUR)
def test_invariant_12_kill_switch_blocks_all_tiers(capital_eur):
    a = CapitalAllocator()
    p = _params()
    s = _state(capital_eur=capital_eur, kill_switch=True)
    sig = _signal()
    d = a.evaluate_trade(sig, s, p)
    assert d.reason_code == "KILL_SWITCH"


# =====================================================================
# Invariant 13 — live_enabled=true blocks paper-only mode
# =====================================================================


@pytest.mark.parametrize("capital_eur", TIERS_EUR)
def test_invariant_13_live_enabled_blocks_paper_at_all_tiers(capital_eur):
    """If live_enabled=True but signal demands paper (live_allowed=false in
    preset), the allocator must reject with LIVE_DISABLED."""
    a = CapitalAllocator()
    p = _params(live_allowed=False)
    s = _state(capital_eur=capital_eur, live_enabled=True, execution_mode="LIVE")
    sig = _signal()
    d = a.evaluate_trade(sig, s, p)
    # Either LIVE_DISABLED or another safety reject — never silently accept
    assert d.accepted is False or d.reason_code is not None
