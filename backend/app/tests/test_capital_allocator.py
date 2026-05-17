"""Unit tests for CapitalAllocator (Phase 1 single-engine refactor).

Covers all 16 outcomes (15 reject reason codes + 1 accept) plus the
capital-aware sliding scale.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.capital_allocator import (
    AllocatorDecision,
    AllocatorParams,
    CapitalAllocator,
    PortfolioState,
    REJECT_CODES,
    TradeSignal,
    category_fee_buffer,
    compute_dynamic_thresholds,
)
from app.services.preset_loader import get_preset


# ---------------- helpers ----------------


def _good_signal(**kw) -> TradeSignal:
    base = dict(
        wallet_address="0xabc",
        wallet_tier="ELITE",
        market_id="0xmarket1",
        side="BUY",
        price=0.5,
        size=10.0,
        notional_usd=5.0,
        ev_lower_bound=0.20,
        copyable_edge=0.70,
        spread=0.01,
        liquidity=10000,
        orderbook_quality="OK",
        category="political",
        copy_delay_seconds=10.0,
        price_deterioration=0.0,
    )
    base.update(kw)
    return TradeSignal(**base)


def _good_state(**kw) -> PortfolioState:
    base = dict(
        capital_total=1000.0,  # FULL tier (no capital-aware tightening)
        capital_available=900.0,
        kill_switch=False,
        exposure_total=0.0,
        exposure_per_wallet={},
        exposure_per_market={},
        open_positions_count=0,
    )
    base.update(kw)
    return PortfolioState(**base)


def _liberal_params(**kw) -> AllocatorParams:
    """Permissive params so the only failures come from the test signal."""
    base = AllocatorParams(
        name="TEST",
        max_total_exposure=0.95,
        max_wallet_exposure=0.50,
        max_market_exposure=0.50,
        max_open_positions=None,
        reserve_buffer=0.05,
        risk_per_trade=0.01,
        min_stake=1.0,
        allowed_tiers=frozenset({"ELITE", "STRONG", "CANDIDATE_ELITE"}),
        min_ev_lb=0.02,
        min_copyable_edge=0.40,
        max_spread_pct=0.04,
        min_liquidity=1000,
        polymarket_fee_pct=0.04,
        min_edge_after_fees=0.01,
        late_entry_thresholds={"political": 600, "crypto_5min": 60, "sport": 300, "macro": 900},
        late_entry_default=300,
        require_orderbook_quality=True,
        live_allowed=False,
        description="test",
        capital_aware_thresholds=False,  # disable slide for unit tests
    )
    return replace(base, **kw)


# ---------------- ACCEPT path ----------------


def test_accept_full_path_returns_sizing():
    a = CapitalAllocator()
    d = a.evaluate_trade(_good_signal(), _good_state(), _liberal_params())
    assert d.accepted is True
    assert d.reason_code is None
    assert d.sizing > 0


def test_accept_sizing_scales_with_capital():
    a = CapitalAllocator()
    s = _good_signal()
    p = _liberal_params()
    d_low = a.evaluate_trade(s, _good_state(capital_total=1000, capital_available=900), p)
    d_high = a.evaluate_trade(s, _good_state(capital_total=10_000, capital_available=9_000), p)
    assert d_high.sizing > d_low.sizing


def test_accept_sizing_quality_multiplier_applies():
    """Multiplier scales sizing by EV_lb — pick EV values that both pass fees gate."""
    a = CapitalAllocator()
    p = _liberal_params(polymarket_fee_pct=0.0, min_edge_after_fees=0.0)  # disable fee gate
    s_low = _good_signal(ev_lower_bound=0.02)   # multiplier 0.6
    s_high = _good_signal(ev_lower_bound=0.20)  # multiplier 1.5
    d_low = a.evaluate_trade(s_low, _good_state(), p)
    d_high = a.evaluate_trade(s_high, _good_state(), p)
    assert d_low.accepted and d_high.accepted
    assert d_high.sizing > d_low.sizing


def test_compute_sizing_clamps_to_min_stake():
    a = CapitalAllocator()
    p = _liberal_params(risk_per_trade=0.0001, min_stake=10.0)
    sizing = a.compute_sizing(_good_signal(), _good_state(capital_total=10_000), p)
    assert sizing >= 10.0


def test_compute_sizing_caps_at_half_capital_available():
    a = CapitalAllocator()
    p = _liberal_params(risk_per_trade=10.0, min_stake=1.0)  # huge risk_per_trade
    state = _good_state(capital_total=1000, capital_available=20)
    sizing = a.compute_sizing(_good_signal(), state, p)
    assert sizing <= 20 * 0.5 * 1.5  # half-cap × max quality multiplier


# ---------------- REJECT codes (15 of them) ----------------


def test_reject_kill_switch():
    a = CapitalAllocator()
    d = a.evaluate_trade(_good_signal(), _good_state(kill_switch=True), _liberal_params())
    assert d.accepted is False
    assert d.reason_code == "KILL_SWITCH"


def test_reject_insufficient_capital_below_min_stake():
    a = CapitalAllocator()
    state = _good_state(capital_total=10, capital_available=0.5)
    p = _liberal_params(min_stake=1.0)
    d = a.evaluate_trade(_good_signal(), state, p)
    assert d.reason_code == "INSUFFICIENT_CAPITAL"


def test_reject_wallet_tier_not_allowed():
    a = CapitalAllocator()
    p = _liberal_params(allowed_tiers=frozenset({"ELITE"}))
    sig = _good_signal(wallet_tier="STRONG")
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "WALLET_TIER"


def test_reject_low_ev():
    a = CapitalAllocator()
    p = _liberal_params(min_ev_lb=0.05)
    sig = _good_signal(ev_lower_bound=0.03)
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "LOW_EV"


def test_reject_low_edge():
    a = CapitalAllocator()
    p = _liberal_params(min_copyable_edge=0.50)
    sig = _good_signal(copyable_edge=0.40)
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "LOW_EDGE"


def test_reject_wide_spread():
    """B16/B17 ELITE-paper bypass: wallet_tier='STRONG' to exercise the spread gate."""
    a = CapitalAllocator()
    p = _liberal_params(max_spread_pct=0.02)
    sig = _good_signal(wallet_tier="STRONG", spread=0.05)
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "WIDE_SPREAD"


def test_reject_low_liquidity():
    """B16/B17 ELITE-paper bypass: wallet_tier='STRONG' to exercise the liquidity gate."""
    a = CapitalAllocator()
    p = _liberal_params(min_liquidity=5000)
    sig = _good_signal(wallet_tier="STRONG", liquidity=100)
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "LOW_LIQUIDITY"


def test_reject_bad_orderbook_when_required():
    """B16/B17 ELITE-paper bypass: wallet_tier='STRONG' to exercise the orderbook gate."""
    a = CapitalAllocator()
    p = _liberal_params(require_orderbook_quality=True)
    for q in ("UNTRADABLE", "BAD", "INSUFFICIENT_DATA"):
        d = a.evaluate_trade(_good_signal(wallet_tier="STRONG", orderbook_quality=q), _good_state(), p)
        assert d.reason_code == "BAD_ORDERBOOK", q


# ============ B16/B17 ELITE-paper bypass contract lock ============
# CRITIQUE: ces deux tests verrouillent le contract du bypass:
#   _elite_paper_bypass = (wallet_tier == 'ELITE'
#                          AND settings.paper_trading_enabled
#                          AND NOT settings.live_enabled)
# Si le bypass live se casse silencieusement, paper PnL ≠ live PnL et
# le bot ouvrira en live des trades qu'il aurait dû reject (orderbook
# manquant sur crypto 5-min → fills désastreux).


def test_elite_paper_bypass_skips_data_quality_gates(monkeypatch):
    """B16/B17 positif: ELITE + paper + live OFF → bypass actif sur spread/liquidity/orderbook.
    2026-05-11: explicit paper_live_strict=False to override env (smoke active runs strict=true)."""
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "paper_trading_enabled", True)
    monkeypatch.setattr(s, "live_enabled", False)
    monkeypatch.setattr(s, "paper_live_strict", False)  # legacy bypass mode

    a = CapitalAllocator()
    # Signal qui fail TOUS les data-quality gates
    bad_sig = _good_signal(
        wallet_tier="ELITE",
        spread=0.50,            # WIDE_SPREAD si bypass off
        liquidity=10,           # LOW_LIQUIDITY si bypass off
        orderbook_quality="UNTRADABLE",  # BAD_ORDERBOOK si bypass off
    )
    p = _liberal_params(
        max_spread_pct=0.02,
        min_liquidity=1000,
        require_orderbook_quality=True,
    )
    d = a.evaluate_trade(bad_sig, _good_state(), p)
    # Bypass actif → ces 3 gates ignorées. La trade peut être accepted ou
    # rejected pour autre raison (ex: late_entry), mais PAS sur ces 3 codes.
    assert d.reason_code not in ("WIDE_SPREAD", "LOW_LIQUIDITY", "BAD_ORDERBOOK"), (
        f"ELITE+paper bypass devrait skip data-quality gates, got {d.reason_code}"
    )


def test_elite_live_re_enables_data_quality_gates(monkeypatch):
    """B16/B17 CRITIQUE: ELITE + live ON → bypass DÉSACTIVÉ, gates réactifs.

    Si ce test fail, le bot prendra des trades live sur des markets sans
    orderbook (crypto 5-min) → slippage/fills incontrôlés.
    """
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "paper_trading_enabled", True)
    monkeypatch.setattr(s, "live_enabled", True)  # LIVE ON → bypass off
    monkeypatch.setattr(s, "paper_live_strict", False)  # ensure isolated from env

    a = CapitalAllocator()
    bad_sig = _good_signal(wallet_tier="ELITE", liquidity=100)
    p = _liberal_params(min_liquidity=5000)
    d = a.evaluate_trade(bad_sig, _good_state(), p)
    assert d.reason_code == "LOW_LIQUIDITY", (
        f"En live, le bypass ELITE doit être OFF — gate liquidity doit reject, got {d.reason_code}"
    )


def test_paper_live_strict_disables_elite_bypass(monkeypatch):
    """A3/A9 (2026-05-11): PAPER_LIVE_STRICT=true neutralise le _elite_paper_bypass.

    Spec opérateur 2026-05-09: "on veut vraiment le paper identique au live".
    Quand le flag strict est ON, paper applique EXACTEMENT les gates live
    sur ELITE (spread, liquidity, orderbook). Si ce test fail, paper redevient
    plus permissif que live et la cadence post-strict est artificielle.
    """
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "paper_trading_enabled", True)
    monkeypatch.setattr(s, "live_enabled", False)
    monkeypatch.setattr(s, "paper_live_strict", True)  # ← strict on → bypass off

    a = CapitalAllocator()
    # Signal qui fail data-quality, mais wallet ELITE (= aurait passé sous bypass)
    bad_spread_sig = _good_signal(wallet_tier="ELITE", spread=0.50)
    p_spread = _liberal_params(max_spread_pct=0.02)
    d_spread = a.evaluate_trade(bad_spread_sig, _good_state(), p_spread)
    assert d_spread.reason_code == "WIDE_SPREAD", (
        f"PAPER_LIVE_STRICT doit reject WIDE_SPREAD pour ELITE, got {d_spread.reason_code}"
    )

    bad_liq_sig = _good_signal(wallet_tier="ELITE", liquidity=10)
    p_liq = _liberal_params(min_liquidity=5000)
    d_liq = a.evaluate_trade(bad_liq_sig, _good_state(), p_liq)
    assert d_liq.reason_code == "LOW_LIQUIDITY", (
        f"PAPER_LIVE_STRICT doit reject LOW_LIQUIDITY pour ELITE, got {d_liq.reason_code}"
    )

    bad_ob_sig = _good_signal(wallet_tier="ELITE", orderbook_quality="UNTRADABLE")
    p_ob = _liberal_params(require_orderbook_quality=True)
    d_ob = a.evaluate_trade(bad_ob_sig, _good_state(), p_ob)
    assert d_ob.reason_code == "BAD_ORDERBOOK", (
        f"PAPER_LIVE_STRICT doit reject BAD_ORDERBOOK pour ELITE, got {d_ob.reason_code}"
    )


def test_paper_live_strict_off_keeps_elite_bypass_default(monkeypatch):
    """A3/A9 (2026-05-11): par défaut PAPER_LIVE_STRICT=false, le bypass
    historique reste actif (compatibilité ascendante). Garantit qu'on
    peut rollback au comportement legacy via env flag sans code change."""
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "paper_trading_enabled", True)
    monkeypatch.setattr(s, "live_enabled", False)
    monkeypatch.setattr(s, "paper_live_strict", False)  # rollback to legacy

    a = CapitalAllocator()
    bad_sig = _good_signal(
        wallet_tier="ELITE", spread=0.50, liquidity=10, orderbook_quality="UNTRADABLE",
    )
    p = _liberal_params(
        max_spread_pct=0.02, min_liquidity=1000, require_orderbook_quality=True,
    )
    d = a.evaluate_trade(bad_sig, _good_state(), p)
    assert d.reason_code not in ("WIDE_SPREAD", "LOW_LIQUIDITY", "BAD_ORDERBOOK"), (
        f"strict=false → bypass legacy doit skip data-quality gates, got {d.reason_code}"
    )


def test_accept_when_orderbook_check_disabled():
    a = CapitalAllocator()
    p = _liberal_params(require_orderbook_quality=False)
    d = a.evaluate_trade(_good_signal(orderbook_quality="BAD"), _good_state(), p)
    assert d.accepted is True


def test_reject_late_entry_per_category():
    a = CapitalAllocator()
    p = _liberal_params(
        late_entry_thresholds={"political": 600, "crypto_5min": 60},
        late_entry_default=300,
    )
    # Crypto: 90s > 60s threshold → reject
    sig_crypto = _good_signal(category="crypto_5min", copy_delay_seconds=90)
    assert a.evaluate_trade(sig_crypto, _good_state(), p).reason_code == "LATE_ENTRY"
    # Political: 90s < 600s → accept
    sig_pol = _good_signal(category="political", copy_delay_seconds=90)
    assert a.evaluate_trade(sig_pol, _good_state(), p).accepted


def test_reject_late_entry_uses_default_for_unknown_category():
    a = CapitalAllocator()
    p = _liberal_params(late_entry_thresholds={"political": 600}, late_entry_default=120)
    sig = _good_signal(category="weird_unknown_cat", copy_delay_seconds=200)
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "LATE_ENTRY"


def test_reject_negative_ev_after_fees():
    """v0.7.6 B12 — dynamic fee `rate * p * (1-p)` per share. Crypto
    at p=0.5 has rate=0.072 → fee per share = 0.072*0.5*0.5 = 0.018.

    PAPER mode (no LIVE_BASE_EV_FLOOR / fee_buffer in min_ev_lb).
    ev_lb=0.012 just above min_ev_lb=0.01 → passes LOW_EV.
    ev_after_fee = 0.012 - 0.018 = -0.006 < 0.01 floor → REJECT.
    """
    a = CapitalAllocator()
    p = _liberal_params(
        min_ev_lb=0.01, min_edge_after_fees=0.01,
        live_allowed=False, min_stake=1.0, polymarket_fee_pct=0.0,
    )
    sig = _good_signal(ev_lower_bound=0.012, category="crypto")
    d = a.evaluate_trade(
        sig,
        _good_state(),
        p,
    )
    assert d.reason_code == "NEGATIVE_EV_AFTER_FEES"


def test_accept_when_ev_after_fees_clears_buffer():
    a = CapitalAllocator()
    # EV_lb 0.20, fee 0.04 ⇒ ev_after_fees = 0.12 ≥ min_edge 0.01
    p = _liberal_params(live_allowed=True, min_stake=5.0, min_edge_after_fees=0.01)
    sig = _good_signal(ev_lower_bound=0.20)
    d = a.evaluate_trade(
        sig,
        _good_state(execution_mode="LIVE", live_enabled=True, min_order_size_status="verified"),
        p,
    )
    assert d.accepted


def test_reject_insufficient_capital_for_stake():
    # If available capital clears min_stake, sizing is clipped to available
    # capital rather than pretending an oversized stake is possible.
    a = CapitalAllocator()
    p = _liberal_params(risk_per_trade=0.5, min_stake=1.0)  # huge per-trade
    state = _good_state(capital_total=1000, capital_available=4)
    d = a.evaluate_trade(_good_signal(), state, p)
    assert d.accepted is True
    assert d.sizing <= state.capital_available


def test_reject_max_total_exposure():
    a = CapitalAllocator()
    p = _liberal_params(max_total_exposure=0.10)  # 10% cap
    state = _good_state(capital_total=1000, capital_available=900, exposure_total=99)
    d = a.evaluate_trade(_good_signal(), state, p)
    assert d.reason_code == "MAX_TOTAL_EXPOSURE"


def test_reject_max_wallet_exposure():
    # v0.7.8 dynamic-caps — sizing now scales down to fit the wallet cap
    # remaining. A reject only fires when remaining < min_stake, in which
    # case the cleaner MIN_ORDER_EXCEEDS_WALLET_CAP code is returned.
    a = CapitalAllocator()
    p = _liberal_params(max_wallet_exposure=0.05, min_stake=2.0)
    state = _good_state(
        capital_total=1000,
        capital_available=900,
        exposure_per_wallet={"0xabc": 49.0},  # cap=50 → remaining=1.0 < min_stake 2.0
    )
    d = a.evaluate_trade(_good_signal(wallet_address="0xabc"), state, p)
    assert d.reason_code == "MIN_ORDER_EXCEEDS_WALLET_CAP"


def test_reject_max_market_exposure():
    # v0.7.8 dynamic-caps — same migration as wallet-cap test above. The
    # legacy MAX_MARKET_EXPOSURE gate is now a safety net only; the
    # primary reject is MIN_ORDER_EXCEEDS_MARKET_CAP when remaining
    # cannot accommodate min_stake.
    a = CapitalAllocator()
    p = _liberal_params(max_market_exposure=0.03, min_stake=2.0)
    state = _good_state(
        capital_total=1000,
        capital_available=900,
        exposure_per_market={"0xmarket1": 29.0},  # cap=30 → remaining=1.0 < min_stake 2.0
    )
    d = a.evaluate_trade(_good_signal(market_id="0xmarket1"), state, p)
    assert d.reason_code == "MIN_ORDER_EXCEEDS_MARKET_CAP"


def test_reject_max_positions_when_set():
    a = CapitalAllocator()
    p = _liberal_params(max_open_positions=3)
    state = _good_state(open_positions_count=3)
    d = a.evaluate_trade(_good_signal(), state, p)
    assert d.reason_code == "MAX_POSITIONS"


def test_no_position_cap_when_unlimited():
    a = CapitalAllocator()
    p = _liberal_params(max_open_positions=None)
    state = _good_state(open_positions_count=999)
    d = a.evaluate_trade(_good_signal(), state, p)
    assert d.accepted is True


# ---------------- order of evaluation ----------------


def test_kill_switch_wins_over_everything():
    """If kill_switch is on, even a state with INSUFFICIENT_CAPITAL still emits KILL_SWITCH."""
    a = CapitalAllocator()
    state = _good_state(kill_switch=True, capital_available=0)
    p = _liberal_params(min_stake=1.0)
    d = a.evaluate_trade(_good_signal(), state, p)
    assert d.reason_code == "KILL_SWITCH"


def test_first_failure_wins():
    """Stack two failures (low EV + wide spread) — only LOW_EV reported (cheaper check first)."""
    a = CapitalAllocator()
    p = _liberal_params(min_ev_lb=0.05, max_spread_pct=0.02)
    sig = _good_signal(ev_lower_bound=0.03, spread=0.05)
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "LOW_EV"  # first failure wins


# ---------------- preset loading ----------------


def test_preset_base_paper_loads():
    p = get_preset("BASE_PAPER")
    assert p.max_total_exposure == 0.95
    assert p.max_open_positions is None
    assert p.live_allowed is False
    assert "ELITE" in p.allowed_tiers
    assert "STRONG" in p.allowed_tiers
    assert p.capital_aware_thresholds is True


def test_preset_base_live_loads():
    p = get_preset("BASE_LIVE")
    assert p.live_allowed is True
    assert p.min_ev_lb == 0.05
    assert p.min_stake == 1.0
    # P0.5 (Round 8, 2026-05-12): raised 0.70 → 0.95 so CAPITAL_TIER_RULES
    # ladder (75-95%) actually takes effect via min(base, tier_cap)=tier_cap.
    assert p.max_total_exposure == 0.95
    assert p.hard_safety_caps  # non-empty


def test_preset_unknown_raises():
    with pytest.raises(KeyError):
        get_preset("PRESET_DOES_NOT_EXIST")


def test_only_two_presets_exist_and_capital_aware():
    """v0.7.2: two neutral presets, no named modes."""
    from app.services.preset_loader import get_all_presets
    names = get_all_presets()
    assert set(names.keys()) == {"BASE_PAPER", "BASE_LIVE"}
    for n, p in names.items():
        assert p.capital_aware_thresholds is True, n


# ---------------- capital-aware sliding scale (5 mandatory) ----------------


def test_dynamic_thresholds_at_50_euros_smalltier_elite_only():
    """v0.7.8 P6 (operator rule 2026-05-05): SMALL tier accepts ELITE ONLY.
    STRONG is integrated at MEDIUM+ (where durations expand too). Capital
    protection at minimum bankroll size."""
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
        min_copyable_edge=0.40,
        max_open_positions=None,
        risk_per_trade=0.01,
    )
    eff = compute_dynamic_thresholds(50.0, base)
    assert eff.allowed_tiers == frozenset({"ELITE"})  # NANO = ELITE only (12-tier 2026-05-09)
    assert eff.max_open_positions == 40  # NANO cap (12-tier 2026-05-09: was 12)


def test_dynamic_thresholds_at_500_usd_small_tier():
    """v0.7.8 P6 12-tier refactor 2026-05-09: at $500 → SMALL tier
    (ELITE GOLD+SILVER only, no STRONG). max_pos=60."""
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG", "CANDIDATE_ELITE"}),
        min_ev_lb=0.02,
        min_copyable_edge=0.40,
        max_open_positions=None,
    )
    eff = compute_dynamic_thresholds(500.0, base)
    # SMALL tier: STRONG NOT in allowed_tiers_intersect → intersected out
    assert eff.allowed_tiers == frozenset({"ELITE"})
    assert eff.max_open_positions == 100  # SMALL cap (12-tier 2026-05-09: was 32)


def test_dynamic_thresholds_at_5000_usd_xl_tier():
    """v0.7.8 P6 12-tier refactor 2026-05-09: at $5000 → XL tier
    (ELITE GOLD+SILVER only, NO STRONG yet — STRONG GOLD overflow starts at
    ELITE_OPEN $10k+). max_pos=180."""
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
        min_copyable_edge=0.40,
        max_open_positions=300,
    )
    eff = compute_dynamic_thresholds(5_000.0, base)
    # XL: STRONG NOT in allowed_tiers (12-tier 2026-05-09 — STRONG only at ELITE_OPEN+)
    assert eff.allowed_tiers == frozenset({"ELITE"})
    assert eff.max_open_positions == 290  # XL cap (was 110)


def test_dynamic_thresholds_at_60000_usd_giga_tier():
    """v0.7.8 P6 12-tier refactor 2026-05-09: at $60k → GIGA tier
    (ELITE all + STRONG GOLD overflow). max_pos=480.
    Spec 2026-05-09: HUGE/INST KEEP STRONG GOLD (no preservation cap)."""
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
        min_copyable_edge=0.40,
        max_open_positions=600,
    )
    eff = compute_dynamic_thresholds(60_000.0, base)
    # GIGA allows STRONG GOLD overflow
    assert eff.allowed_tiers == frozenset({"ELITE", "STRONG"})
    assert eff.max_open_positions == 600  # min(base=600, GIGA cap 720)


def test_dynamic_thresholds_at_70000_usd_huge_tier():
    """v0.7.8 P6 12-tier refactor 2026-05-09: at $70k → HUGE tier
    (ALL ELITE + STRONG GOLD overflow — NO preservation cap, spec 2026-05-09).
    max_pos=640."""
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
        min_copyable_edge=0.40,
        max_open_positions=800,
    )
    eff = compute_dynamic_thresholds(70_000.0, base)
    # HUGE keeps STRONG GOLD overflow (no preservation in 12-tier 2026-05-09)
    assert eff.allowed_tiers == frozenset({"ELITE", "STRONG"})
    assert eff.max_open_positions == 800  # min(base=800, HUGE cap 960)


def test_capital_aware_overrides_base_params_when_tighter():
    """v0.7.8 P6 — tier rules don't apply EV/edge floors anymore (relying
    on ELITE/STRONG strict classification). Base preset's values pass through."""
    base = _liberal_params(
        capital_aware_thresholds=True,
        min_ev_lb=0.02,
        min_copyable_edge=0.40,
    )
    eff = compute_dynamic_thresholds(80.0, base)  # SMALL tier
    # No tier floor → base values stay
    assert eff.min_ev_lb == 0.02
    assert eff.min_copyable_edge == 0.40


def test_capital_aware_does_not_loosen_below_base_params():
    """When base preset is stricter, base wins (no loosening). max_pos
    floor still applies: at SMALL tier_cap=12, base=2, min(2,12)=2."""
    base = _liberal_params(
        capital_aware_thresholds=True,
        min_ev_lb=0.20,
        min_copyable_edge=0.85,
        max_open_positions=2,
    )
    eff = compute_dynamic_thresholds(50.0, base)
    assert eff.min_ev_lb == 0.20  # base wins
    assert eff.min_copyable_edge == 0.85
    assert eff.max_open_positions == 2  # min(2, 12) = 2


def test_capital_aware_disabled_returns_base_unchanged():
    base = _liberal_params(
        capital_aware_thresholds=False,
        allowed_tiers=frozenset({"ELITE", "STRONG", "CANDIDATE_ELITE"}),
        min_ev_lb=0.01,
        max_open_positions=None,
    )
    eff = compute_dynamic_thresholds(20.0, base)  # would be SMALL if enabled
    assert eff is base


def test_capital_aware_strong_rejected_at_small_capital_v0_7_8_p6():
    """v0.7.8 P6 (operator rule 2026-05-05): STRONG REJECTED at SMALL.
    Operator: 'aucun STRONG à ce niveau du bot, on en intégrera en même
    temps qu'on commence à prendre plus de trade et plus long'."""
    a = CapitalAllocator()
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
    )
    state = _good_state(capital_total=50, capital_available=50)
    sig = _good_signal(wallet_tier="STRONG", ev_lower_bound=0.20)
    d = a.evaluate_trade(sig, state, base)
    # P6 (final) — STRONG rejected at SMALL (capital protection)
    assert d.reason_code == "WALLET_TIER", (
        f"STRONG must be rejected by tier at SMALL ; got {d.reason_code}"
    )


def test_capital_aware_strong_passes_at_elite_open_through_inst_v0_7_8_p6():
    """v0.7.8 P6 12-tier refactor 2026-05-09 — STRONG GOLD overflow allowed
    ONLY from ELITE_OPEN ($10k+) up to INST. Rejected at NANO/TINY/MICRO/SMALL/
    MEDIUM/LARGE/XL/XXL (all <$10k tiers are ELITE-only, no STRONG)."""
    a = CapitalAllocator()
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
    )
    sig = _good_signal(wallet_tier="STRONG", ev_lower_bound=0.20)
    # MEDIUM ($1k) — STRONG REJECTED (ELITE-only tier in 12-tier spec 2026-05-09)
    d_medium = a.evaluate_trade(sig, _good_state(capital_total=1500, capital_available=1500), base)
    assert d_medium.reason_code == "WALLET_TIER", f"STRONG must be filtered at MEDIUM ; got {d_medium.reason_code}"
    # XL ($5k) — STRONG REJECTED
    d_xl = a.evaluate_trade(sig, _good_state(capital_total=5_000, capital_available=5_000), base)
    assert d_xl.reason_code == "WALLET_TIER", f"STRONG must be filtered at XL ; got {d_xl.reason_code}"
    # ELITE_OPEN ($15k) — STRONG passes (STRONG GOLD overflow starts here)
    d_eo = a.evaluate_trade(sig, _good_state(capital_total=15_000, capital_available=15_000), base)
    assert d_eo.reason_code != "WALLET_TIER", f"STRONG must pass at ELITE_OPEN ; got {d_eo.reason_code}"
    # GIGA ($40k) — STRONG passes
    d_giga = a.evaluate_trade(sig, _good_state(capital_total=40_000, capital_available=40_000), base)
    assert d_giga.reason_code != "WALLET_TIER", f"STRONG must pass at GIGA ; got {d_giga.reason_code}"
    # HUGE ($70k) — STRONG passes (no preservation cap in 2026-05-09 spec)
    d_huge = a.evaluate_trade(sig, _good_state(capital_total=70_000, capital_available=70_000), base)
    assert d_huge.reason_code != "WALLET_TIER", f"STRONG must pass at HUGE ; got {d_huge.reason_code}"
    # INST ($150k) — STRONG passes
    d_inst = a.evaluate_trade(sig, _good_state(capital_total=150_000, capital_available=150_000), base)
    assert d_inst.reason_code != "WALLET_TIER", f"STRONG must pass at INST ; got {d_inst.reason_code}"


def test_capital_aware_evaluate_at_low_capital_allows_top_elite():
    a = CapitalAllocator()
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
        min_stake=1.0,
        risk_per_trade=0.01,
    )
    state = _good_state(capital_total=50, capital_available=50)
    # v0.7.8 P4 — small-capital safety requires known deadline + category.
    # v0.7.8 B13 — at ≤100€, only ULTRA_SHORT/VERY_SHORT auto-allow, so use
    # a 3-min holding (= ULTRA_SHORT) to keep this an ELITE-acceptance test
    # rather than a duration-filter test.
    sig = _good_signal(
        wallet_tier="ELITE", ev_lower_bound=0.20, copyable_edge=0.70,
        category="Politics", expected_holding_time_hours=0.05,
    )
    d = a.evaluate_trade(sig, state, base)
    # Should accept: ELITE is in intersected allowed_tiers, EV 0.20 > 0.10 floor,
    # copyable_edge 0.70 > 0.60 floor, ULTRA_SHORT auto-allows at 50€.
    assert d.accepted is True


def test_capital_aware_evaluate_at_low_capital_passes_low_ev_elite_v0_7_8_p6():
    """v0.7.8 P6 — SMALL tier no longer applies an EV floor (relies on
    classification quality). Low-EV ELITE may pass the wallet/EV gates
    here. The previous 0.10 floor at small capital was tied to
    ULTRA_SELECTIVE which we removed. EV gating is back to base preset."""
    a = CapitalAllocator()
    base = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
    )
    state = _good_state(capital_total=50, capital_available=50)
    sig = _good_signal(wallet_tier="ELITE", ev_lower_bound=0.06)  # > base 0.02
    d = a.evaluate_trade(sig, state, base)
    # No more 0.10 floor at SMALL → not rejected on LOW_EV
    assert d.reason_code != "LOW_EV"


# ---------------- v0.7 live fee/min-order/capital-aware gates ----------------


def test_high_winrate_negative_ev_never_live_tradable():
    a = CapitalAllocator()
    p = _liberal_params(min_ev_lb=0.05, live_allowed=True, min_stake=5.0)
    sig = _good_signal(ev_lower_bound=-0.01, category="politics")
    d = a.evaluate_trade(
        sig,
        _good_state(execution_mode="LIVE", live_enabled=True, min_order_size_status="verified"),
        p,
    )
    assert d.accepted is False
    assert d.reason_code == "LOW_EV"


def test_live_ev_below_category_fee_buffer_rejected():
    a = CapitalAllocator()
    p = _liberal_params(min_ev_lb=0.02, live_allowed=True, min_stake=5.0)
    sig = _good_signal(ev_lower_bound=0.06, category="crypto")
    d = a.evaluate_trade(
        sig,
        _good_state(execution_mode="LIVE", live_enabled=True, min_order_size_status="verified"),
        p,
    )
    assert d.reason_code == "LOW_EV"
    assert d.reasoning["fee_buffer"] == 0.072


def test_ev_002_to_005_paper_possible_live_rejected():
    a = CapitalAllocator()
    p = _liberal_params(min_ev_lb=0.02, live_allowed=True, min_stake=5.0)
    sig = _good_signal(ev_lower_bound=0.03, category="politics")
    paper = a.evaluate_trade(sig, _good_state(capital_total=500, capital_available=500), p)
    live = a.evaluate_trade(
        sig,
        _good_state(
            capital_total=500,
            capital_available=500,
            execution_mode="LIVE",
            live_enabled=True,
            min_order_size_status="verified",
        ),
        p,
    )
    assert paper.accepted is True
    assert live.accepted is False
    assert live.reason_code == "LOW_EV"


def test_capital_50_live_small_tier_elite_only_v0_7_8_p6():
    """v0.7.8 P6 LOCKED: SMALL = ELITE only (operator rule 2026-05-05).
    STRONG rejected at SMALL even in live mode."""
    a = CapitalAllocator()
    p = _liberal_params(
        capital_aware_thresholds=True,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
        live_allowed=True,
        min_stake=5.0,
    )
    state = _good_state(
        capital_total=50,
        capital_available=50,
        execution_mode="LIVE",
        live_enabled=True,
        min_order_size_status="verified",
    )
    # STRONG REJECTED at SMALL
    strong = a.evaluate_trade(_good_signal(wallet_tier="STRONG", ev_lower_bound=0.20), state, p)
    assert strong.reason_code == "WALLET_TIER", (
        f"STRONG must be REJECTED at SMALL+LIVE, got {strong.reason_code}"
    )
    # LOW EV ELITE: live small-capital floor 0.10 fires
    elite_low = a.evaluate_trade(_good_signal(wallet_tier="ELITE", ev_lower_bound=0.09), state, p)
    assert elite_low.reason_code == "LOW_EV"


def test_capital_50_cannot_ignore_min_order_capital_constraint():
    a = CapitalAllocator()
    p = _liberal_params(min_stake=5.0, live_allowed=True)
    state = _good_state(
        capital_total=50,
        capital_available=4,
        execution_mode="LIVE",
        live_enabled=True,
        min_order_size_status="verified",
    )
    d = a.evaluate_trade(_good_signal(ev_lower_bound=0.20), state, p)
    assert d.reason_code == "INSUFFICIENT_CAPITAL"


def test_capital_500_paper_viable_with_min_order():
    """At $500 (SMALL tier in 12-tier refactor): ELITE GOLD+SILVER only, no STRONG.
    Test ELITE wallet (overrides ELITE-paper bypass via paper_trading_enabled in env)."""
    a = CapitalAllocator()
    p = _liberal_params(min_stake=5.0, capital_aware_thresholds=True)
    state = _good_state(capital_total=500, capital_available=500)
    d = a.evaluate_trade(_good_signal(ev_lower_bound=0.08, wallet_tier="ELITE"), state, p)
    assert d.accepted is True
    assert d.sizing >= 5.0


def test_unknown_category_gets_conservative_fee_warning():
    fee, warning = category_fee_buffer("Uncategorised")
    assert fee == 0.05
    assert warning == "NEEDS_CATEGORY_MAPPING"
    a = CapitalAllocator()
    p = _liberal_params(live_allowed=True, min_stake=5.0, min_edge_after_fees=0.0)
    d = a.evaluate_trade(
        _good_signal(ev_lower_bound=0.08, category="Uncategorised"),
        _good_state(execution_mode="LIVE", live_enabled=True, min_order_size_status="verified"),
        p,
    )
    assert d.accepted is True
    assert d.reasoning["category_warning"] == "NEEDS_CATEGORY_MAPPING"


def test_min_order_unverified_blocks_live():
    a = CapitalAllocator()
    p = _liberal_params(live_allowed=True, min_stake=5.0)
    d = a.evaluate_trade(
        _good_signal(ev_lower_bound=0.20),
        _good_state(execution_mode="LIVE", live_enabled=True, min_order_size_status="unverified"),
        p,
    )
    assert d.reason_code == "MIN_ORDER_UNVERIFIED"


def test_presets_use_same_allocator_engine():
    """Both shipped presets feed the same engine and return AllocatorDecision."""
    a = CapitalAllocator()
    for name in ("BASE_PAPER", "BASE_LIVE"):
        p = get_preset(name)
        state = _good_state(capital_total=500, capital_available=500)
        if p.live_allowed:
            state = _good_state(
                capital_total=500,
                capital_available=500,
                execution_mode="LIVE",
                live_enabled=False,  # forces LIVE_DISABLED, but still returns decision
                min_order_size_status="verified",
            )
        d = a.evaluate_trade(_good_signal(ev_lower_bound=0.20), state, p)
        assert isinstance(d, AllocatorDecision)


def test_zero_leakage_blocked_wallet_never_tradable():
    a = CapitalAllocator()
    p = _liberal_params(
        allowed_tiers=frozenset({"ELITE", "STRONG", "BLOCKED"}),
        live_allowed=True,
        min_stake=5.0,
    )
    d = a.evaluate_trade(
        _good_signal(wallet_tier="BLOCKED", ev_lower_bound=1.0),
        _good_state(execution_mode="LIVE", live_enabled=True, min_order_size_status="verified"),
        p,
    )
    assert d.reason_code == "WALLET_TIER"


def test_no_live_order_path_reachable_when_live_enabled_false():
    a = CapitalAllocator()
    p = _liberal_params(live_allowed=True, min_stake=5.0)
    d = a.evaluate_trade(
        _good_signal(ev_lower_bound=1.0),
        _good_state(execution_mode="LIVE", live_enabled=False, min_order_size_status="verified"),
        p,
    )
    assert d.accepted is False
    assert d.reason_code == "LIVE_DISABLED"


# ---------------- meta ----------------


def test_reject_codes_set_complete():
    """All documented reject codes appear in REJECT_CODES.
    v0.7.6 B13 added CAPITAL_LOCK_TOO_LONG."""
    expected = {
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
        "UNKNOWN_DEADLINE",        # v0.7.8 P4
        "UNKNOWN_CATEGORY",        # v0.7.8 P4
        "MIN_ORDER_EXCEEDS_MARKET_CAP",  # v0.7.8 dynamic-caps
        "MIN_ORDER_EXCEEDS_WALLET_CAP",  # v0.7.8 dynamic-caps
        "DATA_MISSING_MARKET_METADATA",  # v0.7.8 C1
        "ORDERBOOK_UNAVAILABLE",         # v0.7.8 C1
        "MARKET_METADATA_FETCH_FAILED",  # v0.7.8 C1
        "MARKET_METADATA_NOT_FOUND",     # v0.7.8 C1
    }
    assert set(REJECT_CODES) == expected


def test_decision_rejected_property():
    d = AllocatorDecision(False, "LOW_EV")
    assert d.rejected is True
    d2 = AllocatorDecision(True, None, sizing=5.0)
    assert d2.rejected is False
