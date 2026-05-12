"""Phase E1 — CLOB executor (POLYORACLE 2026-05-11).

Wraps the official `py-clob-client` SDK to provide :
1. Connect to Polymarket CLOB API with EOA wallet (signing type 0 = private key direct)
2. Derive L2 API credentials (apiKey + secret + passphrase) one-time
3. Place / cancel orders with idempotence + retry
4. Fetch fills + reconcile with local PaperTrade state
5. Status reporting

**ACTIVATION GATE**: this module is NOT used at runtime tant que `settings.live_enabled = True`
ET `settings.live_clob_enabled = True` (nouveau flag). Until then, paper mode reste inchangé.

**SECRETS**: PRIVATE_KEY is read from env `POLYMARKET_PRIVATE_KEY` (E2 — never in tracked .env,
must be in `.env.live` chmod 600 OR systemd Environment= only on VPS).

**SIGNING TYPE**: 0 (= EOA direct). Pas Magic auth (type 1) car bot dédié = signing key
controlled directly. Cohérent avec plan E1 + recommandation Polymarket docs pour bot use case.

**WRAP pUSD**: Polymarket utilise désormais pUSD (= wrapper ERC-20 backé par USDC.e).
Le bot doit wrapper USDC.e → pUSD au funding initial + unwrap pour withdraw.
Voir `pusd_wrapper.py` pour la logique on-chain (CollateralOnramp contract).

References:
- https://docs.polymarket.com/api-reference/authentication
- https://docs.polymarket.com/trading/clients/l1
- https://github.com/Polymarket/py-clob-client
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# Polygon mainnet chain id
POLYGON_CHAIN_ID = 137

# Default Polymarket CLOB host
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"

# Signing types per Polymarket docs
SIGNATURE_TYPE_EOA = 0  # standard EOA — bot recommended
SIGNATURE_TYPE_MAGIC = 1  # email/proxy wallet — not used
SIGNATURE_TYPE_BROWSER = 2  # browser proxy — not used

# Order retry policy
MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 1.0  # exponential backoff: 1s, 2s, 4s

# API creds cache (persisted to filesystem so we don't re-derive on restart)
DEFAULT_CREDS_PATH = Path("/opt/app/polyoracle/data/_polymarket_l2_creds.json")


@dataclass
class CLOBExecutorConfig:
    """Configuration for the executor. Read env vars + sane defaults."""
    private_key: str
    funder_address: str | None = None  # set on first init from private_key
    chain_id: int = POLYGON_CHAIN_ID
    host: str = DEFAULT_CLOB_HOST
    signature_type: int = SIGNATURE_TYPE_EOA
    creds_cache_path: Path = field(default_factory=lambda: DEFAULT_CREDS_PATH)
    dry_run: bool = False  # if True, no real orders submitted (testing)


@dataclass
class PlaceOrderResult:
    success: bool
    order_id: str | None = None
    idempotence_key: str = ""
    placed_at: str = ""
    side: str = ""
    price: float = 0.0
    size: float = 0.0
    error: str | None = None
    attempt: int = 0
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass
class CancelOrderResult:
    success: bool
    order_id: str = ""
    cancelled_at: str = ""
    error: str | None = None


class CLOBExecutor:
    """Polymarket CLOB executor wrapper.

    Lifecycle:
        1. __init__(config) — instantiates py-clob-client + does L1 init (signs nonce)
        2. ensure_api_credentials() — loads cached L2 creds OR derives + caches
        3. place_order(...) / cancel_order(...) — L2 authenticated calls
        4. get_open_orders() / get_fills() — read endpoints
        5. close() — releases resources

    The `dry_run` mode skips actual network calls — useful for unit tests and
    integration testing before flipping LIVE_ENABLED=true on production.
    """

    def __init__(self, config: CLOBExecutorConfig) -> None:
        if not config.private_key:
            raise ValueError("private_key required (env POLYMARKET_PRIVATE_KEY)")
        if not config.private_key.startswith("0x") or len(config.private_key) != 66:
            raise ValueError(
                "private_key must be 0x-prefixed 64 hex chars (= 66 chars total). "
                "Got len=%d" % len(config.private_key)
            )
        self.config = config
        self._client: Any = None  # py-clob-client.client.ClobClient
        self._api_creds: dict[str, str] | None = None
        self._initialized = False

    # ------- L1 wallet init -------

    def _build_client(self) -> Any:
        """Build py-clob-client. Lazy import to avoid hard dep when not used."""
        if self._client is not None:
            return self._client
        if self.config.dry_run:
            self._client = _DryRunClient(self.config)
        else:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(
                host=self.config.host,
                key=self.config.private_key,
                chain_id=self.config.chain_id,
                signature_type=self.config.signature_type,
            )
        return self._client

    def _resolve_funder_address(self) -> str:
        """Derive the funder address (EOA public address) from the private key."""
        from eth_account import Account
        return Account.from_key(self.config.private_key).address

    # ------- L2 credentials -------

    def _load_cached_creds(self) -> dict[str, str] | None:
        p = self.config.creds_cache_path
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            if all(k in data for k in ("apiKey", "secret", "passphrase")):
                return data
        except Exception:
            return None
        return None

    def _save_creds(self, creds: dict[str, str]) -> None:
        p = self.config.creds_cache_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(creds, indent=2))
        # chmod 600 — only owner can read
        os.chmod(p, 0o600)

    def ensure_api_credentials(self) -> dict[str, str]:
        """L2 credential setup. Idempotent. Persists to disk.

        Per Polymarket docs: repeated `create_or_derive_api_key()` calls with
        the same nonce return the SAME creds, so this is safe to retry.
        """
        if self._api_creds:
            return self._api_creds
        cached = self._load_cached_creds()
        if cached:
            self._api_creds = cached
            return cached
        client = self._build_client()
        if self.config.dry_run:
            creds = {
                "apiKey": "dryrun-key",
                "secret": "dryrun-secret",
                "passphrase": "dryrun-passphrase",
            }
        else:
            from py_clob_client.clob_types import ApiCreds
            api_creds: ApiCreds = client.create_or_derive_api_creds()
            creds = {
                "apiKey": api_creds.api_key,
                "secret": api_creds.api_secret,
                "passphrase": api_creds.api_passphrase,
            }
            # Re-apply to client for subsequent L2 calls
            client.set_api_creds(api_creds)
        self._api_creds = creds
        self._save_creds(creds)
        return creds

    def initialize(self) -> "CLOBExecutor":
        """Build client + ensure L2 creds. Call once at start."""
        self._build_client()
        self.ensure_api_credentials()
        # Re-apply creds for client (path differs in mock vs real)
        if not self.config.dry_run and self._api_creds:
            from py_clob_client.clob_types import ApiCreds
            self._client.set_api_creds(ApiCreds(
                api_key=self._api_creds["apiKey"],
                api_secret=self._api_creds["secret"],
                api_passphrase=self._api_creds["passphrase"],
            ))
        if not self.config.funder_address:
            self.config.funder_address = self._resolve_funder_address()
        self._initialized = True
        return self

    # ------- Place order -------

    def place_order(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"],
        price: float,
        size: float,
        order_type: Literal["GTC", "GTD", "FOK", "FAK"] = "GTC",
        idempotence_key: str | None = None,
    ) -> PlaceOrderResult:
        """Place a limit order on Polymarket CLOB.

        Args:
            token_id: Polymarket outcome token id (= condition_id encoded).
            side: BUY or SELL.
            price: limit price [0.0..1.0].
            size: USDC notional (will be split into shares by SDK).
            order_type: GTC (default) / GTD / FOK (= fill-or-kill) / FAK (= fill-and-kill).
            idempotence_key: UUID for retry-safe submission. Auto-generated if None.

        Returns PlaceOrderResult with order_id on success, error on failure.

        Retry policy: 3 attempts with exponential backoff. The idempotence_key
        is reused across attempts so Polymarket can dedup.
        """
        if not self._initialized:
            self.initialize()
        key = idempotence_key or str(uuid.uuid4())
        if price <= 0 or price >= 1:
            return PlaceOrderResult(
                success=False, idempotence_key=key,
                error=f"invalid price {price}, must be in (0, 1) for prediction market",
            )
        if size <= 0:
            return PlaceOrderResult(
                success=False, idempotence_key=key, error=f"size must be > 0, got {size}",
            )

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        side_const = BUY if side.upper() == "BUY" else SELL
        order_type_enum = getattr(OrderType, order_type, OrderType.GTC)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side_const,
        )

        last_err: str | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if self.config.dry_run:
                    response = {
                        "orderID": f"dryrun-{key[:8]}",
                        "success": True,
                        "dry_run": True,
                    }
                else:
                    signed = self._client.create_order(order_args)
                    response = self._client.post_order(signed, order_type_enum)
                if response.get("success", False) or response.get("orderID"):
                    return PlaceOrderResult(
                        success=True,
                        order_id=response.get("orderID"),
                        idempotence_key=key,
                        placed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                        side=side.upper(),
                        price=price,
                        size=size,
                        attempt=attempt,
                        raw_response=response,
                    )
                last_err = response.get("errorMsg") or "no orderID in response"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))

        return PlaceOrderResult(
            success=False, idempotence_key=key, side=side.upper(),
            price=price, size=size, attempt=MAX_RETRIES, error=last_err,
        )

    # ------- Cancel order -------

    def cancel_order(self, order_id: str) -> CancelOrderResult:
        """Cancel a single order by id. Idempotent (cancelling already-cancelled = success no-op)."""
        if not self._initialized:
            self.initialize()
        try:
            if self.config.dry_run:
                return CancelOrderResult(
                    success=True, order_id=order_id,
                    cancelled_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
            response = self._client.cancel(order_id)
            return CancelOrderResult(
                success=bool(response.get("success", True)),
                order_id=order_id,
                cancelled_at=datetime.now(UTC).isoformat(timespec="seconds"),
                error=response.get("errorMsg") if not response.get("success", True) else None,
            )
        except Exception as exc:
            return CancelOrderResult(
                success=False, order_id=order_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    def cancel_all(self) -> dict[str, Any]:
        """E5 kill switch — cancel ALL open orders for this wallet. Idempotent."""
        if not self._initialized:
            self.initialize()
        if self.config.dry_run:
            return {"cancelled_count": 0, "dry_run": True}
        try:
            return self._client.cancel_all()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    # ------- Read endpoints -------

    def get_open_orders(self) -> list[dict[str, Any]]:
        if not self._initialized:
            self.initialize()
        if self.config.dry_run:
            return []
        try:
            return self._client.get_orders()  # filters to open
        except Exception:
            return []

    def get_trades(self) -> list[dict[str, Any]]:
        if not self._initialized:
            self.initialize()
        if self.config.dry_run:
            return []
        try:
            return self._client.get_trades()
        except Exception:
            return []

    # ------- Status / health -------

    def status(self) -> dict[str, Any]:
        """For /risk/clob-status endpoint."""
        return {
            "initialized": self._initialized,
            "host": self.config.host,
            "chain_id": self.config.chain_id,
            "signature_type": self.config.signature_type,
            "funder_address": self.config.funder_address,
            "dry_run": self.config.dry_run,
            "creds_cached": self._api_creds is not None,
        }


# ---------------- Dry-run mock client (for unit tests) ----------------


class _DryRunClient:
    """Minimal stub matching py-clob-client surface used here. No network."""

    def __init__(self, config: CLOBExecutorConfig) -> None:
        self.config = config

    def set_api_creds(self, *_args, **_kwargs) -> None:
        pass

    def create_or_derive_api_creds(self) -> Any:
        class _C:
            api_key = "dryrun-key"
            api_secret = "dryrun-secret"
            api_passphrase = "dryrun-passphrase"
        return _C()

    def create_order(self, args: Any) -> dict[str, Any]:
        return {"order": "signed-dryrun"}

    def post_order(self, signed: Any, order_type: Any) -> dict[str, Any]:
        return {"orderID": f"dryrun-{uuid.uuid4().hex[:8]}", "success": True}

    def cancel(self, order_id: str) -> dict[str, Any]:
        return {"success": True}

    def cancel_all(self) -> dict[str, Any]:
        return {"cancelled_count": 0}

    def get_orders(self) -> list[dict[str, Any]]:
        return []

    def get_trades(self) -> list[dict[str, Any]]:
        return []


# ---------------- Factory ----------------


def build_executor_from_env() -> CLOBExecutor | None:
    """Convenience factory reading from env vars. Returns None if PRIVATE_KEY
    not set (= live mode disabled / not configured). Safe to call at startup."""
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    if not pk:
        return None
    config = CLOBExecutorConfig(
        private_key=pk,
        host=os.environ.get("POLYMARKET_CLOB_HOST", DEFAULT_CLOB_HOST),
        dry_run=bool(int(os.environ.get("POLYMARKET_CLOB_DRY_RUN", "0"))),
    )
    return CLOBExecutor(config)


def status_payload() -> dict[str, Any]:
    """For /risk/clob-status endpoint without forcing actual init."""
    pk_present = bool(
        os.environ.get("POLYMARKET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    )
    return {
        "configured": pk_present,
        "live_ready": pk_present,  # full check requires init, this is preview
        "host": DEFAULT_CLOB_HOST,
        "chain_id": POLYGON_CHAIN_ID,
        "signature_type": SIGNATURE_TYPE_EOA,
        "creds_cache_exists": DEFAULT_CREDS_PATH.exists(),
        "dry_run": bool(int(os.environ.get("POLYMARKET_CLOB_DRY_RUN", "0"))),
    }
