"""Tests for live_wallet_reader."""
import time
from unittest.mock import MagicMock, patch

import pytest


def test_reader_returns_none_when_no_rpc_url(monkeypatch):
    from app.services.live_wallet_reader import LiveWalletReader
    monkeypatch.delenv("POLYGON_RPC_URL", raising=False)
    monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", "0xA67e3f50b1F0D8B11e0a5C16B55AF4026C50F516")
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    assert reader.read_usdc_balance() is None


def test_reader_returns_none_when_no_wallet(monkeypatch):
    from app.services.live_wallet_reader import LiveWalletReader
    monkeypatch.setenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    monkeypatch.delenv("POLYMARKET_DEPOSIT_WALLET", raising=False)
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    assert reader.read_usdc_balance() is None


def test_reader_cache_within_ttl(monkeypatch):
    from app.services.live_wallet_reader import LiveWalletReader, CACHE_TTL_S
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    # Bypass init by force-setting cache
    reader._cache_value = 12.34
    reader._cache_at = time.time()
    # Should return cached
    assert reader.read_usdc_balance() == 12.34


def test_reader_cache_expires_after_ttl():
    from app.services.live_wallet_reader import LiveWalletReader, CACHE_TTL_S
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    reader._cache_value = 12.34
    reader._cache_at = time.time() - CACHE_TTL_S - 1  # expired
    # Cache expired + no RPC → returns None (init will fail)
    assert reader.read_usdc_balance() is None


def test_status_returns_diagnostic_dict():
    from app.services.live_wallet_reader import LiveWalletReader
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    status = reader.status()
    assert "initialized" in status
    assert "deposit_wallet" in status
    assert "cache_age_s" in status


def test_singleton_returns_same_instance():
    from app.services.live_wallet_reader import LiveWalletReader
    # Reset singleton for test
    LiveWalletReader._instance = None
    inst1 = LiveWalletReader.instance()
    inst2 = LiveWalletReader.instance()
    assert inst1 is inst2


def test_force_refresh_bypasses_cache():
    from app.services.live_wallet_reader import LiveWalletReader
    reader = LiveWalletReader.__new__(LiveWalletReader)
    reader.__init__()
    reader._cache_value = 100.0
    reader._cache_at = time.time()
    # force_refresh=True → tries to init RPC (fails in test env) → returns None
    assert reader.read_usdc_balance(force_refresh=True) is None
