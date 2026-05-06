"""v0.7.6 B12 — dynamic fee engine tests.

Validates :
1. Polymarket formula `fee = C * rate * p * (1 - p)`.
2. Per-category rates match spec (Crypto 7.2 %, Sports 3 %, Geopolitics 0 %, etc.).
3. Geopolitics fee always 0.
4. Extreme prices have minimal absolute fee.
5. ev_after_fee gates correctly.
6. Unknown category falls back to 5 % conservatively.
"""

from __future__ import annotations

import math

from app.services.fee_engine import (
    FEE_RATES,
    compute_dynamic_fee,
    compute_fee_pct_of_notional,
    edge_after_dynamic_fee,
    ev_after_fee,
    get_fee_rate,
    is_high_fee_zone,
    is_low_fee_zone,
)


def test_fee_crypto_p05_max_fee():
    """Crypto rate 0.072 at p=0.50, size=100 -> fee = 100*0.072*0.25 = 1.80 USDC."""
    fee = compute_dynamic_fee("Crypto", 0.5, 100.0)
    assert abs(fee - 1.80) < 1e-6
    # As % of notional (= C * p = 50): 1.80 / 50 = 3.6%
    pct = compute_fee_pct_of_notional("Crypto", 0.5)
    assert abs(pct - 0.036) < 1e-6


def test_fee_geopolitics_zero():
    """Geopolitics: fee is 0 at every price and every size."""
    for p in (0.05, 0.25, 0.50, 0.75, 0.95):
        for c in (1.0, 100.0, 10_000.0):
            assert compute_dynamic_fee("Geopolitics", p, c) == 0.0
    # And the rate:
    assert get_fee_rate("Geopolitics") == 0.0
    assert get_fee_rate("geopolitics") == 0.0


def test_fee_extreme_prices_minimal():
    """At p=0.95, fee should be very small (rate * 0.0475 of notional).
    For Politics rate=0.04: fee = C * 0.04 * 0.95 * 0.05 = C * 0.0019.
    On size=100 -> fee = 0.19. As % of notional (= 95): 0.19/95 = 0.20%.
    """
    fee = compute_dynamic_fee("Politics", 0.95, 100.0)
    expected = 100.0 * 0.04 * 0.95 * 0.05
    assert abs(fee - expected) < 1e-9
    # And p=0.05 (mirror)
    fee_low = compute_dynamic_fee("Politics", 0.05, 100.0)
    expected_low = 100.0 * 0.04 * 0.05 * 0.95
    assert abs(fee_low - expected_low) < 1e-9


def test_ev_after_fee_negative_rejects():
    """If EV_lb is too small relative to the dynamic fee per share, the
    adjusted EV must dip below the conservative 0.01 floor.

    Crypto p=0.5 size=100 fee=1.80 -> per-share fee = 0.018.
    With ev_lb=0.02 -> adjusted = 0.002 < 0.01.
    """
    adjusted = ev_after_fee(
        ev_lower_bound=0.02, category="Crypto", price=0.5, size=100.0
    )
    assert adjusted < 0.01

    # With ev_lb=0.05 same setup -> adjusted = 0.032 > 0.01 (passes)
    passes = ev_after_fee(
        ev_lower_bound=0.05, category="Crypto", price=0.5, size=100.0
    )
    assert passes >= 0.01

    # Geopolitics -> fee=0 -> ev untouched
    geo = ev_after_fee(
        ev_lower_bound=0.02, category="Geopolitics", price=0.5, size=100.0
    )
    assert abs(geo - 0.02) < 1e-9


def test_unknown_category_uses_conservative_default():
    """Unknown category rate = 5% (conservative)."""
    assert get_fee_rate(None) == 0.05
    assert get_fee_rate("") == 0.05
    assert get_fee_rate("Unknown") == 0.05
    assert get_fee_rate("Other") == 0.05
    assert get_fee_rate("RandomCategoryThatDoesntExist") == 0.05


def test_fee_rates_match_polymarket_spec():
    """Spot-check the spec-mandated rates."""
    assert FEE_RATES["crypto"] == 0.072
    assert FEE_RATES["sports"] == 0.030
    assert FEE_RATES["finance"] == 0.040
    assert FEE_RATES["politics"] == 0.040
    assert FEE_RATES["economics"] == 0.050
    assert FEE_RATES["geopolitics"] == 0.0


def test_high_fee_zone_band():
    """Zone [0.45, 0.55] inclusive."""
    assert is_high_fee_zone(0.45) is True
    assert is_high_fee_zone(0.50) is True
    assert is_high_fee_zone(0.55) is True
    assert is_high_fee_zone(0.44) is False
    assert is_high_fee_zone(0.56) is False


def test_low_fee_zone_band():
    """Below 0.30 OR above 0.70."""
    assert is_low_fee_zone(0.10) is True
    assert is_low_fee_zone(0.29) is True
    assert is_low_fee_zone(0.30) is False  # not strict <
    assert is_low_fee_zone(0.50) is False
    assert is_low_fee_zone(0.71) is True
    assert is_low_fee_zone(0.95) is True


def test_edge_after_dynamic_fee_high_zone_penalty():
    """High-fee zone subtracts 0.5pp from edge; low-fee zone untouched."""
    edge_high = edge_after_dynamic_fee(copyable_edge=0.20, category="Politics", price=0.50)
    assert abs(edge_high - 0.195) < 1e-9
    edge_low = edge_after_dynamic_fee(copyable_edge=0.20, category="Politics", price=0.20)
    assert abs(edge_low - 0.20) < 1e-9
    # Never negative
    edge_clamped = edge_after_dynamic_fee(copyable_edge=0.001, category="Politics", price=0.50)
    assert edge_clamped >= 0.0
