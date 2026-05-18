"""Tests for live_wallet_reader (CLOB-backed)."""
import time
from unittest.mock import MagicMock, patch

import pytest


def _reset_singleton():
    from app.services.live_wallet_reader import LiveWalletReader
    LiveWalletReader._instance = None


def test_returns_none_when_missing_env(monkeypatch):
    from app.services.live_wallet_reader import LiveWalletReader
    for v in ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
              "POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER"):
        monkeypatch.delenv(v, raising=False)
    _reset_singleton()
    assert LiveWalletReader.instance().read_usdc_balance() is None


def test_cache_within_ttl_returns_cached():
    from app.services.live_wallet_reader import LiveWalletReader
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    reader._cache_value = 12.34
    reader._cache_at = time.time()
    assert reader.read_usdc_balance() == 12.34


def test_cache_expired_triggers_refresh_returns_none_when_no_creds(monkeypatch):
    from app.services.live_wallet_reader import LiveWalletReader, CACHE_TTL_S
    for v in ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
              "POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER"):
        monkeypatch.delenv(v, raising=False)
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    reader._cache_value = 12.34
    reader._cache_at = time.time() - CACHE_TTL_S - 1
    assert reader.read_usdc_balance() is None


def test_status_returns_diagnostic_dict():
    from app.services.live_wallet_reader import LiveWalletReader
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    s = reader.status()
    assert "initialized" in s and "funder" in s and "cache_age_s" in s and "cache_value" in s


def test_singleton_returns_same_instance():
    from app.services.live_wallet_reader import LiveWalletReader
    _reset_singleton()
    assert LiveWalletReader.instance() is LiveWalletReader.instance()


def test_balance_parsed_from_clob_response():
    """Mock CLOB client → reader returns balance in USD (6 decimals)."""
    from app.services.live_wallet_reader import LiveWalletReader
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    fake_client = MagicMock()
    fake_client.get_balance_allowance.return_value = {"balance": "16394670", "allowances": {}}
    reader._client = fake_client
    reader._init_done = True
    val = reader.read_usdc_balance(force_refresh=True)
    assert val == pytest.approx(16.394670)


def test_invalid_response_returns_none():
    from app.services.live_wallet_reader import LiveWalletReader
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    fake_client = MagicMock()
    fake_client.get_balance_allowance.return_value = {"allowances": {}}  # no balance field
    reader._client = fake_client
    reader._init_done = True
    assert reader.read_usdc_balance(force_refresh=True) is None


def test_exception_returns_none():
    from app.services.live_wallet_reader import LiveWalletReader
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    fake_client = MagicMock()
    fake_client.get_balance_allowance.side_effect = RuntimeError("CLOB down")
    reader._client = fake_client
    reader._init_done = True
    assert reader.read_usdc_balance(force_refresh=True) is None
