"""Live wallet reader — Polymarket collateral balance via CLOB API.

Source-of-truth = `py_clob_client.get_balance_allowance(asset_type=COLLATERAL)`.
Polymarket V2 (POLY_1271) garde le USDC dans un pool agrégé hors-chain → le
solde individuel est tracké par leur API, pas lisible via RPC direct.

USAGE
=====

    reader = LiveWalletReader.instance()
    balance = reader.read_usdc_balance()  # float USD or None on error

INTEGRATION
===========

`compute_effective_paper_capital()` lit ce reader si LIVE_ENABLED. Sinon =
paper_capital + cumul PnL post-T0 (paper).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_TTL_S = 60.0
POLYGON_CHAIN_ID = 137
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"


class LiveWalletReader:
    """Singleton — reads Polymarket collateral balance via CLOB API."""

    _instance: Optional["LiveWalletReader"] = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> "LiveWalletReader":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._client = None
        self._funder = os.environ.get("POLYMARKET_FUNDER")
        self._cache_value: Optional[float] = None
        self._cache_at: float = 0.0
        self._init_done = False

    def _ensure_initialized(self) -> bool:
        if self._init_done:
            return self._client is not None
        self._init_done = True
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            api_key = os.environ.get("POLYMARKET_API_KEY")
            api_secret = os.environ.get("POLYMARKET_API_SECRET")
            api_pass = os.environ.get("POLYMARKET_API_PASSPHRASE")
            pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
            sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "3"))
            host = os.environ.get("CLOB_API_BASE_URL", DEFAULT_CLOB_HOST)

            missing = [
                name for name, val in [
                    ("POLYMARKET_API_KEY", api_key),
                    ("POLYMARKET_API_SECRET", api_secret),
                    ("POLYMARKET_API_PASSPHRASE", api_pass),
                    ("POLYMARKET_PRIVATE_KEY", pk),
                    ("POLYMARKET_FUNDER", self._funder),
                ] if not val
            ]
            if missing:
                logger.warning("LiveWalletReader: missing env vars: %s", missing)
                return False

            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
            self._client = ClobClient(
                host=host,
                key=pk,
                chain_id=POLYGON_CHAIN_ID,
                creds=creds,
                signature_type=sig_type,
                funder=self._funder,
            )
            logger.info("LiveWalletReader initialized: funder=%s sig=%d", self._funder, sig_type)
            return True
        except Exception as exc:
            logger.warning("LiveWalletReader init failed: %r", exc)
            self._client = None
            return False

    def read_usdc_balance(self, force_refresh: bool = False) -> Optional[float]:
        """Returns Polymarket COLLATERAL balance (USDC) in USD, None if API down.

        Cached 60s by default.
        """
        if not force_refresh and self._cache_value is not None:
            if time.time() - self._cache_at < CACHE_TTL_S:
                return self._cache_value

        if not self._ensure_initialized():
            return None

        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            ba = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            raw = ba.get("balance") if isinstance(ba, dict) else None
            if raw is None:
                logger.warning("LiveWalletReader: no balance field in CLOB response: %r", ba)
                return None
            balance_usd = int(raw) / 1e6  # USDC has 6 decimals
            self._cache_value = balance_usd
            self._cache_at = time.time()
            logger.debug("LiveWalletReader: collateral=$%.6f", balance_usd)
            return balance_usd
        except Exception as exc:
            logger.warning("LiveWalletReader read failed: %r", exc)
            return None

    def status(self) -> dict:
        """Diagnostic info."""
        return {
            "initialized": self._init_done and self._client is not None,
            "funder": self._funder,
            "cache_age_s": round(time.time() - self._cache_at, 1) if self._cache_at else None,
            "cache_value": self._cache_value,
        }
