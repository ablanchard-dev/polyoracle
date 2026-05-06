"""v0.7.8 P4 — small-capital safety on UNKNOWN deadline / category.

Per spec: "0 acceptation Unknown category/deadline" at small capital.
Hard-reject below SMALL_CAPITAL_UNKNOWN_THRESHOLD_USD.
"""

from __future__ import annotations

from dataclasses import replace

from app.services.capital_allocator import (
    REJECT_CODES,
    SMALL_CAPITAL_UNKNOWN_THRESHOLD_USD,
    CapitalAllocator, PortfolioState, TradeSignal, AllocatorParams,
)


def _params() -> AllocatorParams:
    return AllocatorParams(
        name="TEST",
        max_total_exposure=0.95,
        max_wallet_exposure=0.50,
        max_market_exposure=0.50,
        max_open_positions=None,
        reserve_buffer=0.05,
        risk_per_trade=0.01,
        min_stake=1.0,
        allowed_tiers=frozenset({"ELITE", "STRONG"}),
        min_ev_lb=0.01,
        min_copyable_edge=0.05,
        max_spread_pct=0.10,
        min_liquidity=100,
        polymarket_fee_pct=0.04,
        min_edge_after_fees=0.01,
        late_entry_thresholds={"crypto_5min": 60, "political": 600, "sport": 300, "macro": 900},
        late_entry_default=300,
        require_orderbook_quality=False,
        live_allowed=False,
        description="test",
        capital_aware_thresholds=False,
    )


def _state(capital: float = 50.0) -> PortfolioState:
    return PortfolioState(
        capital_total=capital,
        capital_available=capital,
        execution_mode="PAPER",
        live_enabled=False,
        min_order_size_status="unverified",
        kill_switch=False,
        exposure_total=0.0,
        exposure_per_wallet={},
        exposure_per_market={},
        open_positions_count=0,
    )


def _signal(**kw):
    base = dict(
        wallet_address="0xa",
        wallet_tier="ELITE",
        market_id="0xm",
        side="BUY",
        price=0.5,
        size=10.0,
        notional_usd=5.0,
        ev_lower_bound=0.10,
        copyable_edge=0.30,
        spread=0.01,
        liquidity=10_000.0,
        liquidity_score=60.0,
        orderbook_quality="OK",
        category="Politics",
        copy_delay_seconds=10.0,
        price_deterioration=0.0,
        expected_holding_time_hours=2.0,
    )
    base.update(kw)
    return TradeSignal(**base)


# ============ UNKNOWN_CATEGORY at small capital ============


def test_unknown_category_rejected_at_50eur():
    """B16/B17 ELITE-paper bypass: wallet_tier='STRONG' to exercise the small-cap unknown gate."""
    a = CapitalAllocator()
    sig = _signal(wallet_tier="STRONG", category=None)
    d = a.evaluate_trade(sig, _state(50.0), _params())
    assert d.accepted is False
    assert d.reason_code == "UNKNOWN_CATEGORY"


def test_unknown_category_rejected_at_100eur():
    """B16/B17 ELITE-paper bypass: wallet_tier='STRONG' to exercise the small-cap unknown gate."""
    a = CapitalAllocator()
    sig = _signal(wallet_tier="STRONG", category="Unknown")
    d = a.evaluate_trade(sig, _state(100.0), _params())
    assert d.accepted is False
    assert d.reason_code == "UNKNOWN_CATEGORY"


def test_unknown_category_passes_at_500eur():
    a = CapitalAllocator()
    sig = _signal(category=None)
    d = a.evaluate_trade(sig, _state(500.0), _params())
    # At 500€ capital >= threshold, Unknown is tolerated and goes through
    # the rest of the pipeline (which may still reject for other reasons,
    # just not UNKNOWN_CATEGORY).
    assert d.reason_code != "UNKNOWN_CATEGORY"
    assert d.reason_code != "UNKNOWN_DEADLINE"


# ============ UNKNOWN_DEADLINE at small capital ============


def test_unknown_deadline_rejected_at_50eur():
    """B16/B17 ELITE-paper bypass: wallet_tier='STRONG' to exercise the small-cap unknown gate."""
    a = CapitalAllocator()
    sig = _signal(wallet_tier="STRONG", expected_holding_time_hours=None)
    d = a.evaluate_trade(sig, _state(50.0), _params())
    assert d.accepted is False
    assert d.reason_code == "UNKNOWN_DEADLINE"


def test_known_deadline_passes_unknown_check():
    a = CapitalAllocator()
    sig = _signal(expected_holding_time_hours=2.0)
    d = a.evaluate_trade(sig, _state(50.0), _params())
    assert d.reason_code != "UNKNOWN_DEADLINE"


def test_unknown_deadline_passes_at_500eur():
    a = CapitalAllocator()
    sig = _signal(expected_holding_time_hours=None)
    d = a.evaluate_trade(sig, _state(500.0), _params())
    assert d.reason_code != "UNKNOWN_DEADLINE"
    assert d.reason_code != "UNKNOWN_CATEGORY"


# ============ Reject codes presence ============


def test_unknown_codes_in_reject_codes_set():
    assert "UNKNOWN_DEADLINE" in REJECT_CODES
    assert "UNKNOWN_CATEGORY" in REJECT_CODES


def test_threshold_constant_is_200_usd():
    assert SMALL_CAPITAL_UNKNOWN_THRESHOLD_USD == 200.0
