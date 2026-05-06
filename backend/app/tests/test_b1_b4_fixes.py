"""v0.7.2 B1 + B4 fix tests.

* B4 — paper engine prefers MFWR.candidate_status over audit.wallet_tier
  so stale tags don't propagate to the allocator as WALLET_TIER rejects.
* B1 — CapitalAllocator uses ``signal.liquidity_score`` (0-100 score)
  when both signal and preset provide one ; falls back to legacy USD
  comparison otherwise.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.models.wallet import MarketFirstWalletRecord
from app.services.capital_allocator import (
    AllocatorParams,
    CapitalAllocator,
    PortfolioState,
    TradeSignal,
)
from app.services.paper_trading_engine import PaperTradingEngine


# ============ B4 ============


def _audit_ctx(**kw):
    base = dict(wallet_tier="WATCH", candidate_status=None)
    base.update(kw)
    return base


def _mfwr(address: str, candidate_status: str) -> MarketFirstWalletRecord:
    return MarketFirstWalletRecord(address=address, candidate_status=candidate_status)


def test_b4_effective_tier_uses_mfwr_first():
    audit = _audit_ctx(wallet_tier="WATCH", candidate_status=None)
    rec = _mfwr("0xabc", "ELITE")
    assert PaperTradingEngine.resolve_effective_tier(audit, rec) == "ELITE"


def test_b4_effective_tier_falls_back_to_audit_candidate_status_then_audit_tier():
    rec = None
    audit_a = _audit_ctx(wallet_tier="WATCH", candidate_status="STRONG")
    assert PaperTradingEngine.resolve_effective_tier(audit_a, rec) == "STRONG"
    audit_b = _audit_ctx(wallet_tier="ELITE", candidate_status=None)
    assert PaperTradingEngine.resolve_effective_tier(audit_b, rec) == "ELITE"


def test_b4_effective_tier_unknown_when_no_data():
    audit = _audit_ctx(wallet_tier=None, candidate_status=None)
    assert PaperTradingEngine.resolve_effective_tier(audit, None) == "UNKNOWN"


def test_b4_mfwr_always_wins_over_audit_even_when_audit_has_value():
    """A WATCH audit tag must NOT override an ELITE truth-pool status."""
    audit = _audit_ctx(wallet_tier="WATCH", candidate_status="WATCH")
    rec = _mfwr("0xabc", "ELITE")
    assert PaperTradingEngine.resolve_effective_tier(audit, rec) == "ELITE"


# ============ B1 ============


def _liberal_params(**kw) -> AllocatorParams:
    base = AllocatorParams(
        name="TEST",
        max_total_exposure=0.95,
        max_wallet_exposure=0.50,
        max_market_exposure=0.50,
        max_open_positions=None,
        reserve_buffer=0.05,
        risk_per_trade=0.01,
        min_stake=1.0,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.02,
        min_copyable_edge=0.40,
        max_spread_pct=0.04,
        min_liquidity=1000,
        polymarket_fee_pct=0.0,
        min_edge_after_fees=0.0,
        late_entry_thresholds={},
        late_entry_default=86_400,  # don't fail on late_entry in these tests
        require_orderbook_quality=True,
        live_allowed=False,
        capital_aware_thresholds=False,
        min_liquidity_score=None,
    )
    return replace(base, **kw)


def _good_signal(**kw) -> TradeSignal:
    base = dict(
        wallet_address="0xabc",
        wallet_tier="ELITE",
        market_id="0xmkt",
        side="BUY",
        price=0.5,
        size=10.0,
        notional_usd=10.0,
        ev_lower_bound=0.20,
        copyable_edge=0.70,
        spread=0.01,
        liquidity=10_000,
        orderbook_quality="OK",
        category="political",
        copy_delay_seconds=10.0,
    )
    base.update(kw)
    return TradeSignal(**base)


def _good_state(**kw) -> PortfolioState:
    base = dict(
        capital_total=1_000.0,
        capital_available=900.0,
    )
    base.update(kw)
    return PortfolioState(**base)


def test_b1_score_gate_passes_when_score_above_min():
    """signal.liquidity_score=50 with gate=30 → ACCEPT (score path)."""
    a = CapitalAllocator()
    p = _liberal_params(min_liquidity_score=30)
    sig = _good_signal(liquidity_score=50, liquidity=15)  # legacy field tiny — score wins
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.accepted is True


def test_b1_score_gate_blocks_when_score_below_min():
    """signal.liquidity_score=20 with gate=30 → LOW_LIQUIDITY (score path)."""
    a = CapitalAllocator()
    p = _liberal_params(min_liquidity_score=30)
    sig = _good_signal(liquidity_score=20, liquidity=99_999)  # legacy huge — score still wins
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "LOW_LIQUIDITY"
    assert d.reasoning.get("gate") == "score"


def test_b1_falls_back_to_legacy_when_score_missing():
    """No liquidity_score on signal → use legacy USD gate."""
    a = CapitalAllocator()
    p = _liberal_params(min_liquidity=1_000, min_liquidity_score=30)
    sig = _good_signal(liquidity=500, liquidity_score=None)
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "LOW_LIQUIDITY"
    assert d.reasoning.get("gate") == "usd_notional_legacy"


def test_b1_legacy_only_when_preset_score_missing():
    """If preset has no min_liquidity_score, legacy gate applies even if signal has score."""
    a = CapitalAllocator()
    p = _liberal_params(min_liquidity=1_000, min_liquidity_score=None)
    sig = _good_signal(liquidity=500, liquidity_score=80)
    d = a.evaluate_trade(sig, _good_state(), p)
    assert d.reason_code == "LOW_LIQUIDITY"
    assert d.reasoning.get("gate") == "usd_notional_legacy"


def test_b1_realistic_smoke_value_passes_paper_preset():
    """Median observed liquidity_score=19.38 in the smoke. Paper preset
    has min_liquidity_score=30, so 19 should fail. With raised score=35
    it should pass."""
    a = CapitalAllocator()
    p = _liberal_params(min_liquidity_score=30)
    fail_sig = _good_signal(liquidity_score=19.38)
    assert a.evaluate_trade(fail_sig, _good_state(), p).reason_code == "LOW_LIQUIDITY"
    pass_sig = _good_signal(liquidity_score=35)
    assert a.evaluate_trade(pass_sig, _good_state(), p).accepted is True


def test_b1_preset_yaml_loads_min_liquidity_score():
    """Both shipped presets carry the new score gate."""
    from app.services.preset_loader import get_preset
    paper = get_preset("BASE_PAPER")
    live = get_preset("BASE_LIVE")
    assert paper.min_liquidity_score == 30
    assert live.min_liquidity_score == 50
