"""P0.7 (Round 8, 2026-05-12) — pUSD/CollateralOnramp address guards.

Verifies:
1. Placeholder addresses are still zero-address (defence-in-depth at EVM level).
2. `address_verification_checklist()` returns a properly-formatted operator guide.
3. `_addresses_verified()` correctly reports unverified state.
4. `wrap()` and `unwrap()` refuse to execute when addresses are placeholders.
5. Once verified addresses are set, the guard flips.
"""

from __future__ import annotations

import pytest

from app.services.pusd_wrapper import (
    COLLATERAL_ONRAMP_ADDRESS_PLACEHOLDER,
    PUSD_ADDRESS_PLACEHOLDER,
    PUSDWrapper,
    address_verification_checklist,
)

_VALID_PK = "0x" + "11" * 32  # any 32-byte hex


def test_placeholders_are_zero_address():
    """Defence-in-depth: even if our software guard is bypassed, sending tx
    to 0x000... will fail at the EVM level (no contract there)."""
    zero = "0x0000000000000000000000000000000000000000"
    assert PUSD_ADDRESS_PLACEHOLDER == zero
    assert COLLATERAL_ONRAMP_ADDRESS_PLACEHOLDER == zero


def test_checklist_returns_expected_structure():
    """Operator checklist must list both unverified addresses + USDC.e (known)."""
    cl = address_verification_checklist()
    assert cl["phase"].startswith("P0.7")
    assert "E8 first live trade" in cl["blocker_for"]
    addrs = cl["addresses_to_verify"]
    assert "pUSD" in addrs
    assert "CollateralOnramp" in addrs
    assert "USDC.e" in addrs
    assert addrs["pUSD"]["status"].startswith("UNVERIFIED")
    assert addrs["CollateralOnramp"]["status"].startswith("UNVERIFIED")
    assert addrs["pUSD"]["env_var"] == "PUSD_ADDRESS"
    assert addrs["CollateralOnramp"]["env_var"] == "COLLATERAL_ONRAMP_ADDRESS"
    assert cl["guard_active"] is True
    assert len(cl["verification_steps"]) >= 5


def test_addresses_verified_false_with_placeholders():
    """Wrapper must NOT consider placeholders as verified."""
    w = PUSDWrapper(private_key=_VALID_PK, dry_run=True)
    assert w._addresses_verified() is False


def test_wrap_refuses_with_placeholder_addresses():
    """wrap() must return error if addresses are placeholders, even in dry_run."""
    w = PUSDWrapper(private_key=_VALID_PK, dry_run=False)  # not dry-run = real path
    result = w.wrap(10.0)
    assert result.success is False
    assert "NOT VERIFIED" in (result.error or "")


def test_unwrap_refuses_with_placeholder_addresses():
    """unwrap() must return error if addresses are placeholders."""
    w = PUSDWrapper(private_key=_VALID_PK, dry_run=False)
    result = w.unwrap(10.0)
    assert result.success is False
    assert "NOT VERIFIED" in (result.error or "")


def test_set_addresses_validates_format():
    """set_addresses() should reject invalid hex addresses."""
    w = PUSDWrapper(private_key=_VALID_PK, dry_run=True)
    with pytest.raises(ValueError, match="Invalid pusd"):
        w.set_addresses(pusd="not-hex", onramp="0x" + "ab" * 20)
    with pytest.raises(ValueError, match="Invalid onramp"):
        w.set_addresses(pusd="0x" + "ab" * 20, onramp="bad")


def test_set_addresses_flips_guard():
    """Once valid addresses are set, the guard reports verified."""
    w = PUSDWrapper(private_key=_VALID_PK, dry_run=True)
    valid_pusd = "0x" + "ab" * 20
    valid_onramp = "0x" + "cd" * 20
    w.set_addresses(pusd=valid_pusd, onramp=valid_onramp)
    assert w._addresses_verified() is True
    assert w.pusd_address == valid_pusd
    assert w.onramp_address == valid_onramp


def test_dry_run_does_not_bypass_guard():
    """Even in dry_run, refused addresses should still block wrap (defence)."""
    w = PUSDWrapper(private_key=_VALID_PK, dry_run=True)
    # No set_addresses called → guard should block
    result = w.wrap(10.0)
    assert result.success is False
    assert "NOT VERIFIED" in (result.error or "")
