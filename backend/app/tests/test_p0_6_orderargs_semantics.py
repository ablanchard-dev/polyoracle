"""P0.6 (Round 8, 2026-05-12) — OrderArgs.size shares vs USDC notional.

Verified against py-clob-client/clob_types.py (installed in venv):
    OrderArgs.size = "Size in terms of the ConditionalToken" = SHARES (verbatim)
    NOT USDC notional, SDK does NOT split.

These tests intercept `OrderArgs` construction to assert the shares value
actually passed to the SDK matches what the caller intended. Pre-fix, the
public surface advertised `size: USDC notional` but passed it raw to
`OrderArgs.size` — would have been catastrophic on first live trade
(e.g. `size=5, price=0.10` → 5 shares × $0.10 = $0.50 instead of $5).
"""

from __future__ import annotations

import pytest

from app.services.clob_executor import (
    CLOBExecutor,
    CLOBExecutorConfig,
    PlaceOrderResult,
)


@pytest.fixture
def dry_executor(tmp_path):
    cfg = CLOBExecutorConfig(
        private_key="0x" + "11" * 32,
        host="https://clob.polymarket.com",
        chain_id=137,
        signature_type=0,
        dry_run=True,
        cache_creds_path=tmp_path / "creds.json",
    )
    return CLOBExecutor(cfg).initialize()


# ---------------- shares semantics (the actual P0.6 fix) ----------------


def test_notional_usd_converts_to_shares_at_mid_price(dry_executor):
    """price=0.50, notional_usd=10 → shares=20 (= 10 / 0.50).

    Pre-P0.6 bug: would have been 10 (raw USDC value).
    Verification via PlaceOrderResult.size (which stores shares from OrderArgs).
    """
    result = dry_executor.place_order(
        token_id="tok-1", side="BUY", price=0.50, notional_usd=10.0,
    )
    assert result.success is True
    assert result.price == 0.50
    assert result.size == 20.0, (
        f"Expected shares=20 (= $10/$0.50), got {result.size}. "
        f"Pre-P0.6 bug: would have been 10 (= raw USDC value)."
    )


def test_notional_usd_at_high_price_yields_fewer_shares(dry_executor):
    """price=0.95, notional_usd=10 → shares ≈ 10.526 (= 10 / 0.95)."""
    result = dry_executor.place_order(
        token_id="tok-2", side="BUY", price=0.95, notional_usd=10.0,
    )
    assert result.success is True
    assert abs(result.size - (10.0 / 0.95)) < 1e-9


def test_notional_usd_at_low_price_yields_more_shares(dry_executor):
    """price=0.10, notional_usd=10 → shares=100 (= 10 / 0.10).

    THE critical case: pre-P0.6 bug would have been 10 shares × $0.10
    = $1 notional, 10× under target.
    """
    result = dry_executor.place_order(
        token_id="tok-3", side="BUY", price=0.10, notional_usd=10.0,
    )
    assert result.success is True
    assert result.size == 100.0


def test_size_shares_passes_through_unchanged(dry_executor):
    """When caller knows shares (e.g. closing position), pass directly."""
    result = dry_executor.place_order(
        token_id="tok-4", side="SELL", price=0.30, size_shares=42.0,
    )
    assert result.success is True
    assert result.size == 42.0


# ---------------- API safety (must require exactly one of notional/shares) ----------------


def test_neither_notional_nor_shares_rejected(dry_executor):
    result = dry_executor.place_order(token_id="t", side="BUY", price=0.5)
    assert result.success is False
    assert "exactly one" in result.error.lower()


def test_both_notional_and_shares_rejected(dry_executor):
    result = dry_executor.place_order(
        token_id="t", side="BUY", price=0.5, notional_usd=5.0, size_shares=10.0,
    )
    assert result.success is False
    assert "exactly one" in result.error.lower()


def test_negative_notional_rejected(dry_executor):
    result = dry_executor.place_order(
        token_id="t", side="BUY", price=0.5, notional_usd=-1.0,
    )
    assert result.success is False
    assert "notional_usd" in result.error.lower()


def test_negative_shares_rejected(dry_executor):
    result = dry_executor.place_order(
        token_id="t", side="BUY", price=0.5, size_shares=-5.0,
    )
    assert result.success is False
    assert "size_shares" in result.error.lower()


# ---------------- PlaceOrderResult notional_usd property ----------------


def test_result_exposes_notional_usd_via_property(dry_executor):
    """PlaceOrderResult.notional_usd = price × size_shares (convenience)."""
    result = dry_executor.place_order(
        token_id="t", side="BUY", price=0.40, notional_usd=20.0,
    )
    assert result.success is True
    # Shares stored: 20 / 0.40 = 50
    assert result.size == 50.0
    # Convenience property reconstructs notional
    assert result.notional_usd == 20.0


