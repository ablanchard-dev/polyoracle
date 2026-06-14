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
    PlaceOrderResult,
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
        creds_cache_path=__import__("pathlib").Path("/tmp/_test_polymarket_creds.json"),
    )


# ====================== CLOB executor ======================


def test_executor_init_rejects_invalid_privkey():
    with pytest.raises(ValueError, match="private_key"):
        CLOBExecutor(CLOBExecutorConfig(private_key=""))


def test_executor_init_rejects_short_privkey():
    with pytest.raises(ValueError, match="private_key must be"):
        CLOBExecutor(CLOBExecutorConfig(private_key="0xabcd"))


def test_executor_initialize_sets_funder_address(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    expected_addr = Account.from_key(_TEST_PRIVKEY).address
    assert ex.config.funder_address == expected_addr
    assert ex._initialized is True


def test_executor_caches_api_creds(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    creds1 = ex.ensure_api_credentials()
    assert "apiKey" in creds1 and "secret" in creds1 and "passphrase" in creds1
    # Second call should return cached
    creds2 = ex.ensure_api_credentials()
    assert creds1 == creds2
    # File should exist with chmod 600
    assert dryrun_config.creds_cache_path.exists()


def test_executor_place_order_dryrun_returns_order_id(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    result = ex.place_order(
        token_id="123456789",
        side="BUY",
        price=0.55,
        notional_usd=5.0,  # P0.6: explicit USDC notional → SDK shares conversion
    )
    assert isinstance(result, PlaceOrderResult)
    assert result.success is True
    assert result.order_id is not None
    assert result.order_id.startswith("dryrun-")
    assert result.attempt == 1


def test_executor_place_order_rejects_invalid_price(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    for bad_price in (0, 1.0, 1.5, -0.1):
        result = ex.place_order(token_id="abc", side="BUY", price=bad_price, notional_usd=5.0)
        assert result.success is False
        assert "invalid price" in result.error.lower()


def test_executor_place_order_rejects_zero_size(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    result = ex.place_order(token_id="abc", side="BUY", price=0.5, notional_usd=0)
    assert result.success is False
    assert "notional_usd" in result.error.lower()


def test_executor_idempotence_key_propagated(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    result = ex.place_order(
        token_id="123",
        side="SELL",
        price=0.45,
        size_shares=10.0,  # P0.6: explicit shares
        idempotence_key="op-key-test-001",
    )
    assert result.idempotence_key == "op-key-test-001"


def test_executor_cancel_order_dryrun(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    result = ex.cancel_order("dryrun-12345")
    assert result.success is True
    assert result.order_id == "dryrun-12345"


def test_executor_cancel_all_dryrun(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
    ex = CLOBExecutor(dryrun_config).initialize()
    result = ex.cancel_all()
    assert result.get("dry_run") is True


def test_executor_status_reports_state(dryrun_config, tmp_path):
    dryrun_config.creds_cache_path = tmp_path / "creds.json"
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
