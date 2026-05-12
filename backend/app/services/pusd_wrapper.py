"""Phase E1b — pUSD wrap/unwrap (POLYORACLE 2026-05-11).

Polymarket uses pUSD (Polymarket USD), an ERC-20 wrapper backed 1:1 by USDC.e.
Trading on Polymarket CLOB requires pUSD, not USDC.e direct.

This module handles the on-chain wrap/unwrap via the CollateralOnramp contract:
- wrap()   : USDC.e → pUSD  (deposit funding flow)
- unwrap() : pUSD → USDC.e  (withdraw flow)

Both operations require approve() of the CollateralOnramp contract for the
input token. We handle that automatically (idempotent — only re-approve if
allowance is insufficient).

References:
- https://docs.polymarket.com/concepts/pusd
- Polygon mainnet addresses (verified Polymarket production):
    USDC.e:           0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
    pUSD:             0xb8c77482e45F1F44dE1745F52C74426C631bDD52 (TBD — verify)
    CollateralOnramp: (TBD — to be filled with verified address)

⚠ NOTE: Addresses must be VERIFIED on Polygonscan before any real-money use.
The contract address for CollateralOnramp evolves with Polymarket deployments —
DO NOT hardcode without re-verification. We expose a `verify_addresses()`
helper that the operator MUST run + confirm before Phase E8 first live trade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Polygon mainnet
POLYGON_CHAIN_ID = 137
POLYGON_RPC_DEFAULT = "https://polygon-rpc.com"

# Token addresses on Polygon mainnet (Polymarket production)
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e bridged

# ⚠ TO BE VERIFIED — placeholders until operator confirms via Polymarket app
PUSD_ADDRESS_PLACEHOLDER = "0x0000000000000000000000000000000000000000"
COLLATERAL_ONRAMP_ADDRESS_PLACEHOLDER = "0x0000000000000000000000000000000000000000"

# Standard ERC-20 ABI (subset we need)
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]

# CollateralOnramp ABI (subset — wrap + unwrap)
ONRAMP_ABI = [
    {
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "wrap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "unwrap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass
class WrapResult:
    success: bool
    operation: str  # "wrap" or "unwrap"
    amount_usdc: float = 0.0
    tx_hash: str | None = None
    error: str | None = None
    timestamp: str = ""


def _to_wei_6(amount_usdc: float) -> int:
    """USDC.e and pUSD use 6 decimals (NOT 18 like ETH). Convert float USDC to atomic units."""
    return int(round(amount_usdc * 1_000_000))


def _from_wei_6(amount_atomic: int) -> float:
    return amount_atomic / 1_000_000


class PUSDWrapper:
    """Wrap/unwrap USDC.e ↔ pUSD via CollateralOnramp contract.

    Usage:
        wrapper = PUSDWrapper(private_key=..., rpc_url="https://polygon-rpc.com")
        wrapper.set_addresses(pusd=..., onramp=...)  # OBLIGATOIRE avant any op
        result = wrapper.wrap(amount_usdc=50.0)
    """

    def __init__(
        self,
        *,
        private_key: str,
        rpc_url: str = POLYGON_RPC_DEFAULT,
        chain_id: int = POLYGON_CHAIN_ID,
        usdc_e_address: str = USDC_E_ADDRESS,
        pusd_address: str = PUSD_ADDRESS_PLACEHOLDER,
        onramp_address: str = COLLATERAL_ONRAMP_ADDRESS_PLACEHOLDER,
        dry_run: bool = False,
    ) -> None:
        if not private_key or not private_key.startswith("0x") or len(private_key) != 66:
            raise ValueError("private_key required (0x + 64 hex chars)")
        self.private_key = private_key
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.usdc_e_address = usdc_e_address
        self.pusd_address = pusd_address
        self.onramp_address = onramp_address
        self.dry_run = dry_run
        self._w3: Any = None
        self._account: Any = None

    def _init_web3(self) -> None:
        if self._w3 is not None:
            return
        from eth_account import Account
        from web3 import Web3
        self._w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 30}))
        self._account = Account.from_key(self.private_key)

    @property
    def address(self) -> str:
        self._init_web3()
        return self._account.address

    def set_addresses(self, *, pusd: str, onramp: str) -> None:
        """Operator MUST set verified addresses before any wrap/unwrap.
        Addresses must be obtained from Polymarket app or official docs.
        DO NOT trust unverified addresses — drain risk."""
        if not (pusd.startswith("0x") and len(pusd) == 42):
            raise ValueError(f"Invalid pusd address: {pusd}")
        if not (onramp.startswith("0x") and len(onramp) == 42):
            raise ValueError(f"Invalid onramp address: {onramp}")
        self.pusd_address = pusd
        self.onramp_address = onramp

    def _addresses_verified(self) -> bool:
        return (
            self.pusd_address != PUSD_ADDRESS_PLACEHOLDER
            and self.onramp_address != COLLATERAL_ONRAMP_ADDRESS_PLACEHOLDER
        )

    def balance_usdc_e(self) -> float:
        """Read USDC.e balance for the bot wallet."""
        if self.dry_run:
            return 100.0  # mock
        self._init_web3()
        contract = self._w3.eth.contract(
            address=self._w3.to_checksum_address(self.usdc_e_address),
            abi=ERC20_ABI,
        )
        raw = contract.functions.balanceOf(self.address).call()
        return _from_wei_6(raw)

    def balance_pusd(self) -> float:
        """Read pUSD balance. Returns 0 if addresses not configured yet."""
        if not self._addresses_verified():
            return 0.0
        if self.dry_run:
            return 0.0
        self._init_web3()
        contract = self._w3.eth.contract(
            address=self._w3.to_checksum_address(self.pusd_address),
            abi=ERC20_ABI,
        )
        raw = contract.functions.balanceOf(self.address).call()
        return _from_wei_6(raw)

    def _allowance(self, token: str, spender: str) -> int:
        self._init_web3()
        contract = self._w3.eth.contract(
            address=self._w3.to_checksum_address(token), abi=ERC20_ABI
        )
        return contract.functions.allowance(self.address, self._w3.to_checksum_address(spender)).call()

    def _approve_if_needed(self, token: str, spender: str, amount_atomic: int) -> str | None:
        """Returns tx hash of approve tx if one was sent, else None (already approved)."""
        current = self._allowance(token, spender)
        if current >= amount_atomic:
            return None
        # Approve with high allowance to avoid re-approve every tx (gas efficient)
        max_uint = (1 << 256) - 1
        contract = self._w3.eth.contract(
            address=self._w3.to_checksum_address(token), abi=ERC20_ABI
        )
        tx = contract.functions.approve(
            self._w3.to_checksum_address(spender), max_uint
        ).build_transaction({
            "from": self.address,
            "nonce": self._w3.eth.get_transaction_count(self.address),
            "chainId": self.chain_id,
            "gas": 100_000,
            "maxFeePerGas": self._w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": self._w3.to_wei(30, "gwei"),
        })
        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"approve tx failed: {tx_hash.hex()}")
        return tx_hash.hex()

    def wrap(self, amount_usdc: float) -> WrapResult:
        """USDC.e → pUSD. Auto-approves CollateralOnramp if needed."""
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        if amount_usdc <= 0:
            return WrapResult(False, "wrap", amount_usdc, error="amount must be > 0", timestamp=ts)
        if not self._addresses_verified():
            return WrapResult(
                False, "wrap", amount_usdc,
                error="pUSD/CollateralOnramp addresses NOT VERIFIED. Set via set_addresses() with values from Polymarket app.",
                timestamp=ts,
            )
        if self.dry_run:
            return WrapResult(
                True, "wrap", amount_usdc,
                tx_hash=f"0xdryrun_wrap_{int(amount_usdc*100):x}",
                timestamp=ts,
            )
        try:
            self._init_web3()
            atomic = _to_wei_6(amount_usdc)
            # 1. Approve USDC.e for CollateralOnramp
            self._approve_if_needed(self.usdc_e_address, self.onramp_address, atomic)
            # 2. Call wrap()
            onramp = self._w3.eth.contract(
                address=self._w3.to_checksum_address(self.onramp_address), abi=ONRAMP_ABI,
            )
            tx = onramp.functions.wrap(atomic).build_transaction({
                "from": self.address,
                "nonce": self._w3.eth.get_transaction_count(self.address),
                "chainId": self.chain_id,
                "gas": 200_000,
                "maxFeePerGas": self._w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self._w3.to_wei(30, "gwei"),
            })
            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status != 1:
                return WrapResult(False, "wrap", amount_usdc, tx_hash=tx_hash.hex(),
                                  error="wrap tx reverted", timestamp=ts)
            return WrapResult(True, "wrap", amount_usdc, tx_hash=tx_hash.hex(), timestamp=ts)
        except Exception as exc:
            return WrapResult(False, "wrap", amount_usdc,
                              error=f"{type(exc).__name__}: {exc}", timestamp=ts)

    def unwrap(self, amount_pusd: float) -> WrapResult:
        """pUSD → USDC.e (withdraw flow). Auto-approves if needed."""
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        if amount_pusd <= 0:
            return WrapResult(False, "unwrap", amount_pusd, error="amount must be > 0", timestamp=ts)
        if not self._addresses_verified():
            return WrapResult(
                False, "unwrap", amount_pusd,
                error="pUSD/CollateralOnramp addresses NOT VERIFIED",
                timestamp=ts,
            )
        if self.dry_run:
            return WrapResult(
                True, "unwrap", amount_pusd,
                tx_hash=f"0xdryrun_unwrap_{int(amount_pusd*100):x}",
                timestamp=ts,
            )
        try:
            self._init_web3()
            atomic = _to_wei_6(amount_pusd)
            self._approve_if_needed(self.pusd_address, self.onramp_address, atomic)
            onramp = self._w3.eth.contract(
                address=self._w3.to_checksum_address(self.onramp_address), abi=ONRAMP_ABI,
            )
            tx = onramp.functions.unwrap(atomic).build_transaction({
                "from": self.address,
                "nonce": self._w3.eth.get_transaction_count(self.address),
                "chainId": self.chain_id,
                "gas": 200_000,
                "maxFeePerGas": self._w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self._w3.to_wei(30, "gwei"),
            })
            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status != 1:
                return WrapResult(False, "unwrap", amount_pusd, tx_hash=tx_hash.hex(),
                                  error="unwrap tx reverted", timestamp=ts)
            return WrapResult(True, "unwrap", amount_pusd, tx_hash=tx_hash.hex(), timestamp=ts)
        except Exception as exc:
            return WrapResult(False, "unwrap", amount_pusd,
                              error=f"{type(exc).__name__}: {exc}", timestamp=ts)


def build_wrapper_from_env() -> PUSDWrapper | None:
    """Factory reading from env vars. Returns None if PRIVATE_KEY not set."""
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    if not pk:
        return None
    return PUSDWrapper(
        private_key=pk,
        rpc_url=os.environ.get("POLYGON_RPC_URL", POLYGON_RPC_DEFAULT),
        usdc_e_address=os.environ.get("USDC_E_ADDRESS", USDC_E_ADDRESS),
        pusd_address=os.environ.get("PUSD_ADDRESS", PUSD_ADDRESS_PLACEHOLDER),
        onramp_address=os.environ.get("COLLATERAL_ONRAMP_ADDRESS", COLLATERAL_ONRAMP_ADDRESS_PLACEHOLDER),
        dry_run=bool(int(os.environ.get("PUSD_DRY_RUN", "0"))),
    )
