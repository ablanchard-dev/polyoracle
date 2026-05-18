"""Live wallet reader — pUSD/USDC balance from Polygon RPC.

Equivalent live de BotState.paper_capital pour le paper. Quand LIVE_ENABLED,
le bot lit le vrai solde wallet pour sizing (paper=live invariant).

Cache 60s pour éviter spam RPC. Fallback gracieux si RPC down.

USAGE
=====

    reader = LiveWalletReader.instance()
    balance = reader.read_usdc_balance()  # returns float USD or None on error

INTEGRATION
===========

`compute_effective_paper_capital()` checks LIVE_ENABLED + LIVE_CAPITAL_SYNC_ENABLED.
Si actif, retourne reader.read_usdc_balance() au lieu de paper_capital + cumul PnL.

ADRESSES
========

USDC.e Polygon (Polymarket utilise pUSD = wrapper ERC-20 backé par USDC.e) :
- USDC.e : 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
- pUSD   : 0x9bf63bC4C36e88c6BCC9c08CAd61C76de9CB7DAa (à vérifier vs Polygonscan)

Pour V2 POLY_1271 : balance check sur DEPOSIT_WALLET (= proxy 0xA67e...).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# USDC.e legacy on Polygon (Polymarket V1 collateral)
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# USDC native on Polygon (Polymarket récent — utilisé maintenant)
USDC_NATIVE_ADDRESS = "0x3c499c542cef5e3811E1192ce70d8cC03d5c3359"
# pUSD (Polymarket wrapper) — set via env to skip if not deployed
PUSD_ADDRESS = os.environ.get("POLYMARKET_PUSD_ADDRESS", "")

# Standard ERC20 balanceOf ABI fragment
ERC20_BALANCEOF_ABI = [{
    "constant": True,
    "inputs": [{"name": "_owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "balance", "type": "uint256"}],
    "type": "function",
}]

CACHE_TTL_S = 60.0


class LiveWalletReader:
    """Singleton — reads Polymarket deposit wallet pUSD + USDC.e balance."""

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
        self._w3 = None
        self._usdc_contract = None
        self._pusd_contract = None
        self._deposit_wallet = os.environ.get("POLYMARKET_DEPOSIT_WALLET")
        self._cache_value: Optional[float] = None
        self._cache_at: float = 0.0
        self._init_done = False

    def _ensure_initialized(self) -> bool:
        if self._init_done:
            return self._w3 is not None
        self._init_done = True
        try:
            from web3 import Web3
            rpc_url = os.environ.get("POLYGON_RPC_URL")
            if not rpc_url:
                logger.warning("LiveWalletReader: POLYGON_RPC_URL not set, disabled")
                return False
            if not self._deposit_wallet:
                logger.warning("LiveWalletReader: POLYMARKET_DEPOSIT_WALLET not set")
                return False
            self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            # Skip is_connected() check (Web3 v7 quirks with POA). Try real call.
            try:
                _ = self._w3.eth.block_number  # test RPC works
            except Exception as exc:
                logger.warning("LiveWalletReader: RPC test call failed: %r", exc)
                self._w3 = None
                return False
            checksum_wallet = Web3.to_checksum_address(self._deposit_wallet)
            self._deposit_wallet = checksum_wallet
            # USDC.e legacy + USDC native (Polymarket récent)
            self._usdc_e_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_E_ADDRESS),
                abi=ERC20_BALANCEOF_ABI,
            )
            self._usdc_native_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_NATIVE_ADDRESS),
                abi=ERC20_BALANCEOF_ABI,
            )
            # pUSD optional — only if env set
            self._pusd_contract = None
            if PUSD_ADDRESS:
                try:
                    self._pusd_contract = self._w3.eth.contract(
                        address=Web3.to_checksum_address(PUSD_ADDRESS),
                        abi=ERC20_BALANCEOF_ABI,
                    )
                except Exception as exc:
                    logger.warning("pUSD address invalid: %r", exc)
            logger.info(
                "LiveWalletReader initialized: wallet=%s rpc=%s",
                self._deposit_wallet, rpc_url[:40] + "...",
            )
            return True
        except Exception as exc:
            logger.warning("LiveWalletReader init failed: %r", exc)
            self._w3 = None
            return False

    def read_usdc_balance(self, force_refresh: bool = False) -> Optional[float]:
        """Returns pUSD + USDC.e combined balance USD, None if RPC down.

        Cached 60s by default.
        """
        # Cache check
        if not force_refresh and self._cache_value is not None:
            if time.time() - self._cache_at < CACHE_TTL_S:
                return self._cache_value

        if not self._ensure_initialized():
            return None

        try:
            # USDC.e (legacy)
            usdc_e_wei = self._usdc_e_contract.functions.balanceOf(self._deposit_wallet).call()
            usdc_e = usdc_e_wei / 1e6

            # USDC native (Polymarket récent)
            usdc_native_wei = self._usdc_native_contract.functions.balanceOf(self._deposit_wallet).call()
            usdc_native = usdc_native_wei / 1e6

            # pUSD (optional)
            pusd = 0.0
            if self._pusd_contract is not None:
                try:
                    pusd_wei = self._pusd_contract.functions.balanceOf(self._deposit_wallet).call()
                    pusd = pusd_wei / 1e6
                except Exception:
                    pass

            total = usdc_e + usdc_native + pusd
            self._cache_value = total
            self._cache_at = time.time()
            logger.debug(
                "LiveWalletReader: usdc.e=%.4f usdc_native=%.4f pusd=%.4f total=%.4f",
                usdc_e, usdc_native, pusd, total,
            )
            return total
        except Exception as exc:
            logger.warning("LiveWalletReader read failed: %r", exc)
            return None

    def status(self) -> dict:
        """Diagnostic info."""
        return {
            "initialized": self._init_done and self._w3 is not None,
            "deposit_wallet": self._deposit_wallet,
            "cache_age_s": round(time.time() - self._cache_at, 1) if self._cache_at else None,
            "cache_value": self._cache_value,
        }
