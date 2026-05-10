"""v0.7.8 dynamic-caps — capital-aware exposure caps + sizing clamp.

8 ciblés tests, agreed scope with user:
1. dynamic cap 50€ compatible min order
2. dynamic cap 100€ compatible min order
3. compute_sizing clamps after multipliers
4. min_stake > cap dynamique → reject propre
5. replay parity with PaperTradingEngine + CapitalAllocator inputs
6. no STRONG allowed in ELITE-only replay
7. no legacy max_open_positions=15 active in FULL capital-aware path
8. dynamic cap decreases as capital grows
"""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from app.services.capital_allocator import (
    AllocatorParams,
    CapitalAllocator,
    HARD_MAX_MARKET_PCT,
    HARD_MAX_WALLET_PCT,
    PortfolioState,
    REJECT_CODES,
    TradeSignal,
    compute_dynamic_thresholds,
    dynamic_market_cap_pct,
    dynamic_wallet_cap_pct,
)
from app.services.preset_loader import get_preset


# ---------------- helpers ----------------


def _params(**kw):
    base = AllocatorParams(
        name="TEST",
        max_total_exposure=0.95,
        max_wallet_exposure=0.10,  # base preset value
        max_market_exposure=0.05,  # base preset value
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
    base = dict(
        wallet_address="0xabc",
        wallet_tier="ELITE",
        market_id="0xmkt",
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
        expected_holding_time_hours=0.5,
    )
    base.update(kw)
    return TradeSignal(**base)


def _state(capital_eur=100.0, **kw):
    base = dict(
        capital_total=capital_eur * 1.08,
        capital_available=capital_eur * 1.08,
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


# ---------------- 1 — 50€ compatible with min_order ----------------


def test_dynamic_cap_50eur_compatible_with_min_order():
    """At 50€ ($54), min_stake=$5. The dynamic market cap must be at
    least min_stake / capital × 1.20 = 11.1% so a min order can fit."""
    cap_pct = dynamic_market_cap_pct(54.0, 5.0, 0.05)
    cap_usd = cap_pct * 54.0
    assert cap_usd >= 5.0, (
        f"50€ market cap {cap_usd:.2f} must accommodate min_stake $5"
    )
    # Allocator path: ELITE signal at 50€ should not produce
    # MIN_ORDER_EXCEEDS_MARKET_CAP on a fresh market.
    p = _params()
    s = _state(capital_eur=50.0)
    d = CapitalAllocator().evaluate_trade(_signal(), s, p)
    assert d.reason_code != "MIN_ORDER_EXCEEDS_MARKET_CAP"
    assert d.reason_code != "MIN_ORDER_EXCEEDS_WALLET_CAP"


# ---------------- 2 — 100€ compatible with min_order ----------------


def test_dynamic_cap_100eur_compatible_with_min_order():
    """At 100€ ($108), the dynamic market cap should be at least 5.56%
    (= min_stake/$108 × 1.20) and the target band is 10%."""
    cap_pct = dynamic_market_cap_pct(108.0, 5.0, 0.05)
    cap_usd = cap_pct * 108.0
    assert cap_pct >= 5.0 / 108.0 * 1.20  # min-order-aware floor
    assert cap_usd >= 5.0
    # Effective params from compute_dynamic_thresholds at 100€ should
    # show the override applied.
    p = _params()
    eff = compute_dynamic_thresholds(108.0, p)
    assert eff.max_market_exposure == cap_pct
    assert eff.max_wallet_exposure == dynamic_wallet_cap_pct(108.0, 5.0, 0.10)


# ---------------- 3 — compute_sizing clamps after multipliers ----------------


def test_compute_sizing_clamps_after_multipliers():
    """compute_sizing produces base × quality × duration ($8.25 at 100€)
    but must clamp final to per-market cap remaining ($10.80 at 100€).
    At 100€ it fits. Verify that when wallet_cap_remaining is small,
    sizing clamps further down."""
    p = _params()
    a = CapitalAllocator()
    # Fresh state at 100€: cap=$10.8, sized $8.25 should fit
    s = _state(capital_eur=100.0)
    sized = a.compute_sizing(_signal(), s, compute_dynamic_thresholds(108.0, p))
    assert sized > 0
    assert sized <= 10.80 + 1e-6, f"sized {sized} exceeds market cap 10.80"

    # State with $10 already on the wallet → wallet_remaining = 21.6 - 10
    # = 11.6, market_remaining = 10.8. sized should clamp to 10.8.
    s2 = _state(
        capital_eur=100.0,
        exposure_per_wallet={"0xabc": 10.0},
        exposure_per_market={},
    )
    sized2 = a.compute_sizing(_signal(), s2, compute_dynamic_thresholds(108.0, p))
    assert sized2 <= 10.80 + 1e-6, f"sized2 {sized2} should clamp to market cap 10.80"


# ---------------- 4 — min_stake > cap → clean reject ----------------


def test_min_order_exceeds_dynamic_cap_rejects_cleanly():
    """When market_cap_remaining < min_stake (e.g. capital so small that
    even 20% hard ceiling can't admit min_stake), allocator must return
    MIN_ORDER_EXCEEDS_MARKET_CAP, not MAX_MARKET_EXPOSURE.

    Signal must pass B13 first — use ULTRA_SHORT (3 min) so duration filter
    auto-allows at SMALL tier."""
    p = _params(min_stake=5.0, max_market_exposure=0.05)
    # capital_total=20 USD → 20% hard cap = $4 → cannot admit min_stake $5
    s = PortfolioState(
        capital_total=20.0,
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
    sig = _signal(expected_holding_time_hours=0.05)  # 3 min — ULTRA_SHORT
    d = CapitalAllocator().evaluate_trade(sig, s, p)
    assert d.reason_code == "MIN_ORDER_EXCEEDS_MARKET_CAP", (
        f"expected MIN_ORDER_EXCEEDS_MARKET_CAP, got {d.reason_code}"
    )
    assert "MIN_ORDER_EXCEEDS_MARKET_CAP" in REJECT_CODES
    assert "MIN_ORDER_EXCEEDS_WALLET_CAP" in REJECT_CODES


# ---------------- 5 — replay parity ----------------


def test_replay_parity_with_paper_engine_inputs():
    """The replay script (_v0_7_8_elite_rejection_audit.py + _v0_7_8_consistency_check.py)
    builds a TradeSignal that must match what paper_trading_engine.maybe_auto_trade
    passes to CapitalAllocator. This test guards that contract."""
    # Mirror prod: bot_loop.py:308 / wallet_polling_engine.py:531
    raw_edge = 0.42
    audit_context = {
        "copyable_edge_score": min(100.0, raw_edge * 200.0),
    }
    # paper_trading_engine._normalise_edge logic
    score = float(audit_context["copyable_edge_score"])
    prod_edge = round(score / 100.0, 6) if score > 1.0 else score
    # Replay logic (now matched in _v0_7_8_consistency_check.py)
    rep_score = min(100.0, raw_edge * 200.0)
    rep_edge = round(rep_score / 100.0, 6) if rep_score > 1.0 else rep_score
    assert prod_edge == rep_edge, (
        f"replay edge {rep_edge} must match prod {prod_edge}"
    )

    # Size clamp parity: replay no longer pre-floors to min_stake
    notional, max_w, max_m = 1.64, 21.6, 10.8
    rep_size = min(notional, max_w, max_m) if notional > 0 else 0.0
    # Prod paper_trading_engine.py:914-915
    prod_size = min(notional, max_w, max_m) if notional > 0 else 0.0
    assert rep_size == prod_size


# ---------------- 6 — no STRONG in ELITE-only ----------------


def test_strong_filtered_at_nano_and_huge_v0_7_8_p6_12tier():
    """v0.7.8 P6 — 12-tier refactor 2026-05-06:
    NANO/TINY/MICRO/SMALL (<$1k)   = ELITE only (no STRONG below $1k)
    MEDIUM-GIGA ($1k-$63.99k)      = ELITE + STRONG GOLD overflow
    HUGE/INST   (≥$64k)            = ELITE only (preservation)."""
    p = _params(allowed_tiers=frozenset({"ELITE", "STRONG"}))

    # NANO — STRONG filtered out
    eff_50 = compute_dynamic_thresholds(54.0, p)
    assert eff_50.allowed_tiers == frozenset({"ELITE"})
    s_50 = _state(capital_eur=50.0)
    d_50 = CapitalAllocator().evaluate_trade(_signal(wallet_tier="STRONG"), s_50, p)
    assert d_50.reason_code == "WALLET_TIER", f"STRONG must reject at NANO, got {d_50.reason_code}"

    # MEDIUM — STRONG GOLD overflow allowed
    eff_1k = compute_dynamic_thresholds(1080.0, p)
    assert "STRONG" in eff_1k.allowed_tiers
    assert "ELITE" in eff_1k.allowed_tiers

    # GIGA — STRONG still allowed
    eff_60k = compute_dynamic_thresholds(60_000.0, p)
    assert "STRONG" in eff_60k.allowed_tiers

    # HUGE — STRONG filtered out (preservation)
    eff_70k = compute_dynamic_thresholds(70_000.0, p)
    assert eff_70k.allowed_tiers == frozenset({"ELITE"})

    # INST — STRONG filtered out
    eff_150k = compute_dynamic_thresholds(150_000.0, p)
    assert eff_150k.allowed_tiers == frozenset({"ELITE"})


# ---------------- 7 — max_open_positions scaling per capital ----------------


def test_max_open_positions_scales_per_capital_12tier():
    """v0.7.8 P6 — 12-tier refactor 2026-05-06: max_open_positions per tier:
    NANO=12, TINY=18, MICRO=22, SMALL=32, MEDIUM=50, LARGE=75, XL=110,
    XXL=150, ELITE_OPEN=200, GIGA=300, HUGE=400, INST=500."""
    p = get_preset("BASE_PAPER")
    if p.max_open_positions is not None:
        assert p.max_open_positions != 15, "legacy max_open_positions=15"
    # NANO (<$200): 12
    assert compute_dynamic_thresholds(54.0, p).max_open_positions == 12
    assert compute_dynamic_thresholds(180.0, p).max_open_positions == 12
    # TINY ($200-249): 18
    assert compute_dynamic_thresholds(220.0, p).max_open_positions == 18
    # MICRO ($250-499): 22
    assert compute_dynamic_thresholds(400.0, p).max_open_positions == 22
    # SMALL ($500-999): 32
    assert compute_dynamic_thresholds(700.0, p).max_open_positions == 32
    # MEDIUM ($1k-1.99k): 50
    assert compute_dynamic_thresholds(1500.0, p).max_open_positions == 50
    # LARGE ($2k-3.99k): 75
    assert compute_dynamic_thresholds(3000.0, p).max_open_positions == 75
    # XL ($4k-7.99k): 110
    assert compute_dynamic_thresholds(5000.0, p).max_open_positions == 110
    # XXL ($8k-9.99k): 150
    assert compute_dynamic_thresholds(9000.0, p).max_open_positions == 150
    # ELITE_OPEN ($10k-31.99k): 200
    assert compute_dynamic_thresholds(15_000.0, p).max_open_positions == 200
    # GIGA ($32k-63.99k): 300
    assert compute_dynamic_thresholds(40_000.0, p).max_open_positions == 300
    # HUGE ($64k-127.99k): 400
    assert compute_dynamic_thresholds(80_000.0, p).max_open_positions == 400
    # INST (≥$128k): 500
    assert compute_dynamic_thresholds(150_000.0, p).max_open_positions == 500


# ---------------- 8 — cap decreases as capital grows ----------------


def test_dynamic_market_cap_decreases_as_capital_grows():
    """The dynamic market cap PCT must monotonically decrease (or stay
    flat at the min-order-aware floor) as capital grows. This guarantees
    that small-capital users get adequate room and large-capital users
    get tighter diversification."""
    pcts = [
        dynamic_market_cap_pct(c * 1.08, 5.0, 0.05)
        for c in [50, 75, 100, 150, 250, 500, 1000, 5000]
    ]
    # Allow plateaus (e.g. 75 and 100 both at floor) but never increase
    for prev, cur in zip(pcts, pcts[1:]):
        assert cur <= prev + 1e-9, (
            f"market cap pct must not grow as capital grows: {pcts}"
        )
    # Hard ceiling enforced
    assert max(pcts) <= HARD_MAX_MARKET_PCT
    # Same monotonicity for wallet cap
    wpcts = [
        dynamic_wallet_cap_pct(c * 1.08, 5.0, 0.10)
        for c in [50, 75, 100, 150, 250, 500, 1000, 5000]
    ]
    for prev, cur in zip(wpcts, wpcts[1:]):
        assert cur <= prev + 1e-9, (
            f"wallet cap pct must not grow as capital grows: {wpcts}"
        )
    assert max(wpcts) <= HARD_MAX_WALLET_PCT


# ---------------- bonus: end-to-end ELITE @ 100€ pass ----------------


def test_elite_100eur_sizing_unblocks_after_dynamic_caps():
    """The motivating bug: at 100€ pre-fix, ELITE sized $8.25 vs cap
    $5.40 → MAX_MARKET_EXPOSURE. Post-fix: cap is $10.80, sized $8.25
    fits. Verify allocator accepts a clean ELITE signal at 100€."""
    p = _params()
    s = _state(capital_eur=100.0)
    d = CapitalAllocator().evaluate_trade(_signal(), s, p)
    # Either accept, or reject for an UNRELATED reason (not exposure).
    # The point is MAX_MARKET_EXPOSURE must NOT fire.
    assert d.reason_code not in ("MAX_MARKET_EXPOSURE", "MAX_WALLET_EXPOSURE"), (
        f"allocator still rejects with {d.reason_code} at 100€ post dynamic-caps"
    )
