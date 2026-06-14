"""Phase E1 — Tests CLOB executor + pUSD wrapper (dry-run mode).

Verifies the executor's surface without making real network calls. Real
network/CLOB testing happens later in Phase E8 (1st live $5 trade controlled).
"""

from __future__ import annotations

import pytest
from eth_account import Account

from app.services.clob_executor import (
    CLOBExecutor,
    CLOBExecutorConfig,
    build_executor_from_env,
)
from app.services.pusd_wrapper import (
    COLLATERAL_ONRAMP_ADDRESS_PLACEHOLDER,
    PUSD_ADDRESS_PLACEHOLDER,
    PUSDWrapper,
    USDC_E_ADDRESS,
    WrapResult,
    _from_wei_6,
    _to_wei_6,
)


# Test private key — derived from a fixed mnemonic, NEVER funded, NEVER use in production
_TEST_PRIVKEY = "0x" + "11" * 32


@pytest.fixture
def dryrun_config():
    return CLOBExecutorConfig(
        private_key=_TEST_PRIVKEY, dry_run=True,
        cache_creds_path=__import__("pathlib").Path("/tmp/_test_polymarket_creds.json"),
    )


# ====================== CLOB executor ======================


# ---------------------------------------------------------------------------
# Removed (audit 2026-06): the executor order-placement tests here targeted the
# pre-"live v2" API — constructor privkey validation, ensure_api_credentials(),
# and place_order(price=, size_shares=). The live-v2 refactor (d1d7e05) replaced
# that with market orders (place_order(notional_usd=...)) routed through a
# `py_clob_client_v2` SDK that was never delivered, so the current path is not
# exercisable in dry-run/CI (place_order returns success=False without a live
# orderbook). Removed rather than rewritten to keep the suite honest. The
# still-valid surface — cancel_all, status, build_executor_from_env, and the full
# pUSD wrapper — is kept below. Polymarket integration is frozen paper-only.
# ---------------------------------------------------------------------------


def test_executor_cancel_all_dryrun(dryrun_config, tmp_path):
    dryrun_config.cache_creds_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    result = ex.cancel_all()
    assert result.get("dry_run") is True


def test_executor_status_reports_state(dryrun_config, tmp_path):
    dryrun_config.cache_creds_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    status = ex.status()
    assert status["initialized"] is True
    assert status["dry_run"] is True
    assert status["signature_type"] == dryrun_config.signature_type


def test_build_executor_from_env_none_if_no_privkey(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    assert build_executor_from_env() is None


# ====================== pUSD wrapper ======================


def test_wei_conversion_6_decimals():
    assert _to_wei_6(1.0) == 1_000_000
    assert _to_wei_6(100.5) == 100_500_000
    assert _from_wei_6(1_000_000) == 1.0


def test_wrapper_init_rejects_invalid_privkey():
    with pytest.raises(ValueError):
        PUSDWrapper(private_key="0xabcd")


def test_wrapper_addresses_not_verified_initially():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    assert not w._addresses_verified()


def test_wrapper_set_addresses_validates_format():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    with pytest.raises(ValueError):
        w.set_addresses(pusd="not-a-hex", onramp="0x" + "0" * 40)
    with pytest.raises(ValueError):
        w.set_addresses(pusd="0x" + "0" * 40, onramp="invalid")


def test_wrapper_wrap_fails_without_verified_addresses():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    result = w.wrap(50.0)
    assert result.success is False
    assert "NOT VERIFIED" in result.error


def test_wrapper_wrap_dryrun_succeeds_after_set_addresses():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    w.set_addresses(pusd="0x" + "1" * 40, onramp="0x" + "2" * 40)
    result = w.wrap(50.0)
    assert result.success is True
    assert result.operation == "wrap"
    assert result.amount_usdc == 50.0
    assert result.tx_hash is not None


def test_wrapper_unwrap_dryrun():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    w.set_addresses(pusd="0x" + "1" * 40, onramp="0x" + "2" * 40)
    result = w.unwrap(25.0)
    assert result.success is True
    assert result.operation == "unwrap"


def test_wrapper_rejects_zero_amount():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    w.set_addresses(pusd="0x" + "1" * 40, onramp="0x" + "2" * 40)
    assert w.wrap(0).success is False
    assert w.wrap(-1).success is False
    assert w.unwrap(0).success is False


def test_wrapper_balance_usdc_e_dryrun():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    assert w.balance_usdc_e() == 100.0  # mock


def test_wrapper_balance_pusd_zero_without_verified():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    assert w.balance_pusd() == 0.0


def test_wrapper_address_derived_correctly():
    w = PUSDWrapper(private_key=_TEST_PRIVKEY, dry_run=True)
    expected = Account.from_key(_TEST_PRIVKEY).address
    assert w.address == expected


def test_usdc_e_address_constant_matches_polygon():
    """Sanity: USDC.e address must match Circle's bridged USDC on Polygon."""
    assert USDC_E_ADDRESS.lower() == "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
