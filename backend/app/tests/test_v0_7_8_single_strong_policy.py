"""v0.7.8 P3 — single STRONG strict policy tests."""

from __future__ import annotations

from unittest.mock import patch

import os

from app.services.single_strong_policy import (
    SingleStrongStrictContext,
    is_single_strong_strict_eligible,
)


def _ctx(**kw) -> SingleStrongStrictContext:
    base = dict(
        wallet_tier="STRONG",
        capital_total_eur=50.0,
        market_speed_class="FAST",
        duration_bucket="ULTRA_SHORT",
        category="Crypto",
        deadline_known=True,
        copyable_edge=0.35,
        ev_after_fee=0.05,
        spread=0.01,
        orderbook_quality="GOOD",
    )
    base.update(kw)
    return SingleStrongStrictContext(**base)


def _enable_policy():
    """Reload module with policy enabled. Returns the reloaded module."""
    import importlib
    import app.services.single_strong_policy as mod
    with patch.dict(os.environ, {"SINGLE_STRONG_STRICT_POLICY": "1"}):
        importlib.reload(mod)
        return mod


def _disable_policy():
    import importlib
    import app.services.single_strong_policy as mod
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SINGLE_STRONG_STRICT_POLICY", None)
        importlib.reload(mod)
        return mod


def test_policy_disabled_by_default():
    mod = _disable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx())
    assert eligible is False
    assert reason == "policy_disabled"


def test_policy_enabled_full_pass():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx())
    assert eligible is True, f"should pass all 10 gates, got: {reason}"
    assert reason is None
    _disable_policy()


def test_elite_uses_default_path():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(wallet_tier="ELITE"))
    assert eligible is False
    assert "elite" in reason.lower()
    _disable_policy()


def test_capital_above_max_blocks():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(capital_total_eur=200.0))
    assert eligible is False
    assert "capital_above_max" in reason
    _disable_policy()


def test_market_speed_must_be_fast():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(market_speed_class="SLOW"))
    assert eligible is False
    assert "speed_not_fast" in reason
    _disable_policy()


def test_duration_must_be_short():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(duration_bucket="MEDIUM"))
    assert eligible is False
    assert "duration_not_short" in reason
    _disable_policy()


def test_unknown_category_blocks():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(category="Unknown"))
    assert eligible is False
    assert "category_unknown" in reason
    eligible2, reason2 = mod.is_single_strong_strict_eligible(_ctx(category=None))
    assert eligible2 is False
    assert "category_unknown" in reason2
    _disable_policy()


def test_unknown_deadline_blocks():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(deadline_known=False))
    assert eligible is False
    assert "deadline_unknown" in reason
    _disable_policy()


def test_edge_below_min_blocks():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(copyable_edge=0.20))
    assert eligible is False
    assert "edge_below_min" in reason
    _disable_policy()


def test_ev_after_fee_below_min_blocks():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(ev_after_fee=0.005))
    assert eligible is False
    assert "ev_after_fee_below_min" in reason
    _disable_policy()


def test_spread_too_wide_blocks():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(spread=0.10))
    assert eligible is False
    assert "spread_too_wide" in reason
    _disable_policy()


def test_bad_orderbook_blocks():
    mod = _enable_policy()
    eligible, reason = mod.is_single_strong_strict_eligible(_ctx(orderbook_quality="BAD"))
    assert eligible is False
    assert "orderbook_bad" in reason
    _disable_policy()
