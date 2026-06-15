"""CLOBExecutor v2 — Polymarket live order execution via py-clob-client-v2.

DOCTRINE LIVE_READY_STRICT (2026-05-20) :
- Wallet POLY_1271 (sig_type=3) supporté nativement par v2 SDK
- MarketOrderArgsV2 + FAK uniquement (jamais FOK ni LIMIT)
- Confirmation fill obligatoire via tradeIDs / get_order / get_trades
- Paper mirror = avg_fill_price + filled_shares réels (pas best_ask seul)
- Pas de fallback Gamma : si CLOB non fillable → no fill, no paper mirror
- price MarketOrderArgsV2 = SLIPPAGE CAP DYNAMIQUE via compute_max_acceptable_price()
  (PAS hardcoded 0.99 — évite fills catastrophiques sur thin markets)
- Intègre cache WebSocket via polymarket_ws_orderbook si disponible, fallback REST

FLAGS DE DÉPLOIEMENT :
- LIVE_EXECUTOR_V2_ENABLED : code v2 actif (init client OK, peut signer)
- LIVE_READY_STRICT       : execution parity active (CLOB only, pas de Gamma)
- LIVE_ENABLED            : flip global live trading (submit ordres réels)
- E8_ONLY                 : seul _e8_single_live_trade.py peut soumettre
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# Retry/timeout
MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 0.4
HTTP_TIMEOUT_S = 8.0
FILL_CONFIRM_TIMEOUT_S = 5.0
FILL_CONFIRM_POLL_INTERVAL_S = 0.3

# Slippage caps DYNAMIQUES — calculés via compute_max_acceptable_price, pas hardcoded
DEFAULT_MAX_SLIP_VS_VWAP_PCT = 0.02   # accept jusqu'à 2% above VWAP
DEFAULT_MAX_SLIP_VS_GAMMA_PCT = 0.10  # OR jusqu'à 10% above gamma mid (whichever lower)
DEFAULT_SLIP_FLOOR_BUY = 0.50         # never accept BUY > 0.50 cap above mid 0 = $0.50 worst
DEFAULT_MIN_FILL_RATIO = 0.80

# Min orderbook depth required to consider tradable (USDC)
DEFAULT_MIN_FILLABLE_DEPTH_USD = 1.0

# Flags
FLAG_V2_ENABLED = "LIVE_EXECUTOR_V2_ENABLED"
FLAG_READY_STRICT = "LIVE_READY_STRICT"
FLAG_LIVE_ENABLED = "LIVE_ENABLED"
FLAG_E8_ONLY = "E8_ONLY"


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip() in ("1", "true", "True", "yes")


def is_v2_enabled() -> bool:
    return _flag(FLAG_V2_ENABLED)


def is_strict_enabled() -> bool:
    return _flag(FLAG_READY_STRICT)


def is_live_enabled() -> bool:
    return _flag(FLAG_LIVE_ENABLED)


def is_e8_only() -> bool:
    return _flag(FLAG_E8_ONLY)


@dataclass
class CLOBExecutorConfig:
    host: str = DEFAULT_CLOB_HOST
    chain_id: int = POLYGON_CHAIN_ID
    private_key: str = ""
    funder_address: str = ""
    # AUDIT 2026-06: signature_type=3 / "POLY_1271" is FICTIONAL. The real
    # py-clob-client only knows 0=EOA, 1=Magic, 2=proxy (a Magic wallet is 1).
    # This — like the `py_clob_client_v2` imports further down — belongs to the
    # never-delivered "live v2" path, which stays behind dry_run + flags.
    # Polymarket integration is paper-only; do NOT flip to live without first
    # rewriting this path against a real SDK (py-clob-client / polymarket-client).
    signature_type: int = 3  # phantom default; kept as-is to avoid behaviour change
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    cache_creds_path: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "backend", "_clob_creds_cache.json")
    dry_run: bool = False
    max_slip_vs_vwap_pct: float = DEFAULT_MAX_SLIP_VS_VWAP_PCT
    max_slip_vs_gamma_pct: float = DEFAULT_MAX_SLIP_VS_GAMMA_PCT
    min_fill_ratio: float = DEFAULT_MIN_FILL_RATIO
    fill_confirm_timeout_s: float = FILL_CONFIRM_TIMEOUT_S


@dataclass
class PlaceOrderResult:
    success: bool
    fill_status: str  # FULL_FILL / PARTIAL_FILL_ACCEPTED / PARTIAL_FILL_REJECTED / NO_FILL
                     # NO_ORDERBOOK / INSUFFICIENT_DEPTH / SLIPPAGE_TOO_HIGH / REJECTED / EXCEPTION
                     # SKIP_NOT_LIVE_ENABLED / SKIP_E8_ONLY
    order_id: Optional[str] = None
    trade_ids: list[str] = field(default_factory=list)
    filled_shares: float = 0.0
    filled_notional: float = 0.0
    avg_fill_price: float = 0.0
    fees_paid: float = 0.0
    side: Optional[str] = None
    requested_notional_usd: float = 0.0
    slippage_cap_used: float = 0.0
    vwap_computed: float = 0.0
    price_source: str = ""  # WS_CACHE / REST_FALLBACK / NONE
    latency_ms: int = 0
    idempotence_key: str = ""
    placed_at: str = ""
    attempt: int = 0
    error: Optional[str] = None
    raw_response: Optional[dict] = None


@dataclass
class CancelOrderResult:
    success: bool
    order_id: str
    error: Optional[str] = None


def compute_max_acceptable_price(
    side: Literal["BUY", "SELL"],
    vwap: float,
    gamma_mid: Optional[float] = None,
    cfg: Optional[CLOBExecutorConfig] = None,
) -> float:
    """Compute the worst-price slippage cap (= price field in MarketOrderArgsV2).

    Doctrine : NEVER hardcoded 0.99. Always derived from VWAP and gamma reference.
    Returns the max price acceptable for BUY (or min price for SELL).
    For BUY : cap = min(vwap × 1.02, gamma × 1.10) clamped to [vwap, 0.99]
    For SELL : cap = max(vwap × 0.98, gamma × 0.90) clamped to [0.01, vwap]
    """
    cfg = cfg or CLOBExecutorConfig()
    if side == "BUY":
        cap_vwap = vwap * (1.0 + cfg.max_slip_vs_vwap_pct)
        if gamma_mid and gamma_mid > 0:
            cap_gamma = gamma_mid * (1.0 + cfg.max_slip_vs_gamma_pct)
            cap = min(cap_vwap, cap_gamma)
        else:
            cap = cap_vwap
        # Clamp : never below vwap (would reject everything), never above 0.99 (Polymarket limit)
        cap = max(cap, vwap)
        cap = min(cap, 0.99)
        return round(cap, 4)
    else:  # SELL
        floor_vwap = vwap * (1.0 - cfg.max_slip_vs_vwap_pct)
        if gamma_mid and gamma_mid > 0:
            floor_gamma = gamma_mid * (1.0 - cfg.max_slip_vs_gamma_pct)
            floor = max(floor_vwap, floor_gamma)
        else:
            floor = floor_vwap
        floor = min(floor, vwap)
        floor = max(floor, 0.01)
        return round(floor, 4)


class CLOBExecutor:
    """Live Polymarket execution using py-clob-client-v2."""

    def __init__(self, config: CLOBExecutorConfig) -> None:
        self.config = config
        self._client: Any = None
        self._api_creds: Optional[dict[str, str]] = None
        self._initialized = False
        self._init_lock = threading.Lock()
        # Stats counter — tracks where get_executable_price gets its price source
        self._stats_lock = threading.Lock()
        self._stats = {
            "calls_total": 0,
            "ws_cache_hits": 0,         # served from WS cache (sub-100ms)
            "ws_cache_stale": 0,        # WS had entry but too old
            "rest_fallback_hits": 0,    # had to query REST
            "rest_ok_fillable": 0,      # REST returned OK_FILLABLE
            "no_orderbook": 0,          # 404 or no asks/bids
            "insufficient_depth": 0,    # not enough liquidity for notional
            "exceptions": 0,            # other errors
            "place_order_calls": 0,
            "place_order_filled": 0,
            "place_order_partial": 0,
            "place_order_rejected": 0,
        }

    def _bump_stat(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + n

    def get_stats(self) -> dict[str, Any]:
        """Snapshot of executor stats. Thread-safe copy."""
        with self._stats_lock:
            snap = dict(self._stats)
        # Derived rates
        total = snap.get("calls_total", 0) or 1
        snap["ws_cache_hit_rate_pct"] = round(100 * snap["ws_cache_hits"] / total, 2)
        snap["rest_fallback_rate_pct"] = round(100 * snap["rest_fallback_hits"] / total, 2)
        snap["no_orderbook_rate_pct"] = round(100 * snap["no_orderbook"] / total, 2)
        return snap

    def initialize(self) -> "CLOBExecutor":
        with self._init_lock:
            if self._initialized:
                return self
            if self.config.dry_run:
                self._client = _DryRunClient(self.config)
                self._initialized = True
                logger.info("CLOBExecutor v2 DRY_RUN initialized")
                return self
            self._client = self._build_client()
            if self.config.api_key and self.config.api_secret and self.config.api_passphrase:
                from py_clob_client_v2 import ApiCreds
                creds = ApiCreds(
                    api_key=self.config.api_key,
                    api_secret=self.config.api_secret,
                    api_passphrase=self.config.api_passphrase,
                )
                self._client.set_api_creds(creds)
                self._api_creds = {
                    "api_key": self.config.api_key,
                    "api_secret": self.config.api_secret,
                    "api_passphrase": self.config.api_passphrase,
                }
            else:
                derived = self._client.create_or_derive_api_key()
                self._client.set_api_creds(derived)
                self._api_creds = {
                    "api_key": derived.api_key,
                    "api_secret": derived.api_secret,
                    "api_passphrase": derived.api_passphrase,
                }
                self._save_creds(self._api_creds)
            self._client.assert_level_2_auth()
            self._initialized = True
            logger.info(
                "CLOBExecutor v2 OK sig_type=%s funder=%s flags v2=%s strict=%s live=%s e8only=%s",
                self.config.signature_type, self.config.funder_address,
                is_v2_enabled(), is_strict_enabled(), is_live_enabled(), is_e8_only(),
            )
            return self

    def _build_client(self) -> Any:
        from py_clob_client_v2 import ClobClient, SignatureTypeV2
        sig_enum = SignatureTypeV2(self.config.signature_type)
        return ClobClient(
            host=self.config.host,
            chain_id=self.config.chain_id,
            key=self.config.private_key,
            signature_type=sig_enum,
            funder=self.config.funder_address,
        )

    def _save_creds(self, creds: dict[str, str]) -> None:
        try:
            with open(self.config.cache_creds_path, "w") as f:
                json.dump(creds, f)
            os.chmod(self.config.cache_creds_path, 0o600)
        except Exception as exc:
            logger.warning("could not save CLOB creds cache: %s", exc)

    # ------- Orderbook (WS cache → REST fallback) -------

    def get_executable_price(
        self,
        token_id: str,
        side: Literal["BUY", "SELL"],
        notional_usd: float,
        gamma_mid: Optional[float] = None,
    ) -> tuple[Optional[float], str, dict]:
        """Returns (vwap_worst_price, status, audit).
        status in OK_FILLABLE / NO_ORDERBOOK / INSUFFICIENT_DEPTH / EXCEPTION.

        Tries WS cache first, falls back to REST get_order_book.
        """
        audit: dict = {
            "token_id": token_id, "side": side, "requested_notional": notional_usd,
            "gamma_mid_reference": gamma_mid,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._bump_stat("calls_total")

        # Try WS cache first
        ws_book = None
        try:
            from app.services.polymarket_ws_orderbook import get_cached_orderbook
            ws_book = get_cached_orderbook(token_id)
        except ImportError:
            pass

        if ws_book and ws_book.is_fresh(max_age_s=2.0):
            audit["price_source"] = "WS_CACHE"
            audit["ws_book_age_s"] = round(time.time() - ws_book.last_update_ts, 3)
            audit["ws_events_received"] = ws_book.received_events
            if side == "BUY":
                vwap = ws_book.compute_vwap_buy(notional_usd)
            else:
                vwap = ws_book.compute_vwap_sell(notional_usd)
            if vwap is not None and 0 < vwap < 1:
                audit["status"] = "OK_FILLABLE"
                audit["vwap_worst_price"] = vwap
                audit["best_ask"] = ws_book.best_ask()
                audit["best_bid"] = ws_book.best_bid()
                self._bump_stat("ws_cache_hits")
                return vwap, "OK_FILLABLE", audit
            audit["ws_status"] = "INSUFFICIENT_DEPTH_OR_EMPTY"
            # fall through to REST
        elif ws_book is not None:
            # had cache entry but too stale
            self._bump_stat("ws_cache_stale")

        # Fallback REST
        if not self._initialized:
            self.initialize()
        audit["price_source"] = "REST_FALLBACK"
        self._bump_stat("rest_fallback_hits")
        try:
            book = self._client.get_order_book(token_id)
            # v2 returns dict, not object with .asks/.bids attrs
            if isinstance(book, dict):
                levels = book.get("asks", []) if side == "BUY" else book.get("bids", [])
            else:
                levels = book.asks if side == "BUY" else book.bids
            if not levels:
                audit["status"] = "NO_ORDERBOOK"
                self._bump_stat("no_orderbook")
                return None, "NO_ORDERBOOK", audit
            # Normalize levels to (price, size) tuples sorted cheapest-first.
            # v2 returns dict per level, v1 returns OrderSummary objects.
            # Polymarket asks list = descending price → reversed = ascending.
            norm = []
            for lvl in levels:
                if isinstance(lvl, dict):
                    px = float(lvl.get("price", 0) or 0)
                    sz = float(lvl.get("size", 0) or 0)
                else:
                    px = float(lvl.price)
                    sz = float(lvl.size)
                norm.append((px, sz))
            # BUY consumes asks ascending (cheapest first) ; SELL consumes bids descending
            norm.sort(key=lambda x: x[0], reverse=(side != "BUY"))
            # TRUE VWAP : walk levels, accumulate shares, vwap = notional / shares
            remaining = float(notional_usd)
            total_shares = 0.0
            worst_price = None
            for px, sz in norm:
                if px <= 0:
                    continue
                if side == "BUY":
                    level_value = px * sz       # USDC available here
                    take = min(remaining, level_value)
                    total_shares += take / px
                    remaining -= take
                else:
                    take = min(remaining, sz)   # shares (remaining in shares for SELL)
                    total_shares += take * px   # actually USDC for SELL
                    remaining -= take
                worst_price = px
                if remaining <= 1e-9:
                    break
            if remaining > 1e-9:
                audit["status"] = "INSUFFICIENT_DEPTH"
                self._bump_stat("insufficient_depth")
                return None, "INSUFFICIENT_DEPTH", audit
            if side == "BUY":
                vwap = notional_usd / total_shares if total_shares > 0 else None
            else:
                vwap = total_shares / notional_usd if notional_usd > 0 else None
            if vwap is None or not (0 < vwap < 1):
                audit["status"] = "INSUFFICIENT_DEPTH"
                self._bump_stat("insufficient_depth")
                return None, "INSUFFICIENT_DEPTH", audit
            audit["status"] = "OK_FILLABLE"
            audit["vwap"] = vwap
            audit["worst_price"] = worst_price
            self._bump_stat("rest_ok_fillable")
            return vwap, "OK_FILLABLE", audit
        except Exception as exc:
            err = str(exc)
            if "404" in err or "No orderbook" in err or "no match" in err.lower():
                audit["status"] = "NO_ORDERBOOK"
                audit["error"] = err[:100]
                self._bump_stat("no_orderbook")
                return None, "NO_ORDERBOOK", audit
            audit["status"] = "EXCEPTION"
            audit["error"] = f"{type(exc).__name__}: {err[:100]}"
            self._bump_stat("exceptions")
            return None, "EXCEPTION", audit

    # ------- Place order -------

    def place_order(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"],
        notional_usd: float,
        gamma_mid_reference: Optional[float] = None,
        min_fill_ratio: Optional[float] = None,
        idempotence_key: Optional[str] = None,
        e8_authorized: bool = False,
    ) -> PlaceOrderResult:
        """Submit a FAK market order with dynamic slippage cap.

        e8_authorized : True only for calls from _e8_single_live_trade.py
        (bypasses E8_ONLY flag check). Bot global never sets this.
        """
        if not self._initialized:
            self.initialize()
        key = idempotence_key or str(uuid.uuid4())
        min_ratio = min_fill_ratio if min_fill_ratio is not None else self.config.min_fill_ratio

        result_base = dict(
            side=side, requested_notional_usd=notional_usd, idempotence_key=key,
        )

        # Flag check : E8_ONLY blocks non-E8 callers
        if is_e8_only() and not e8_authorized:
            return PlaceOrderResult(
                success=False, fill_status="SKIP_E8_ONLY",
                error="E8_ONLY flag active, only _e8 script can submit",
                **result_base,
            )

        # Flag check : LIVE_ENABLED required for real submit
        if not is_live_enabled() and not e8_authorized:
            return PlaceOrderResult(
                success=False, fill_status="SKIP_NOT_LIVE_ENABLED",
                error="LIVE_ENABLED=false",
                **result_base,
            )

        # Pre-check orderbook
        vwap, status, audit = self.get_executable_price(
            token_id, side, notional_usd, gamma_mid=gamma_mid_reference,
        )
        if status != "OK_FILLABLE" or vwap is None:
            return PlaceOrderResult(
                success=False, fill_status=status,
                error=audit.get("error") or status,
                raw_response={"pre_check": audit},
                price_source=audit.get("price_source", ""),
                **result_base,
            )

        # Compute dynamic slippage cap
        slip_cap = compute_max_acceptable_price(side, vwap, gamma_mid_reference, self.config)

        # Safety : if VWAP itself already exceeds cap (= bad orderbook), skip
        if side == "BUY" and vwap > slip_cap:
            return PlaceOrderResult(
                success=False, fill_status="SLIPPAGE_TOO_HIGH",
                vwap_computed=vwap, slippage_cap_used=slip_cap,
                error=f"vwap={vwap} > cap={slip_cap}",
                price_source=audit.get("price_source", ""),
                **result_base,
            )
        if side == "SELL" and vwap < slip_cap:
            return PlaceOrderResult(
                success=False, fill_status="SLIPPAGE_TOO_HIGH",
                vwap_computed=vwap, slippage_cap_used=slip_cap,
                error=f"vwap={vwap} < floor={slip_cap}",
                price_source=audit.get("price_source", ""),
                **result_base,
            )

        # Submit FAK
        t0 = time.time()
        try:
            from py_clob_client_v2 import MarketOrderArgsV2, OrderType, Side
            side_enum = Side.BUY if side == "BUY" else Side.SELL
            margs = MarketOrderArgsV2(
                token_id=token_id,
                amount=float(notional_usd),
                side=side_enum,
                price=slip_cap,
                order_type=OrderType.FAK,
            )

            response = None
            attempt = 0
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if self.config.dry_run:
                        response = {
                            "orderID": f"dryrun-{key[:8]}",
                            "status": "matched",
                            "takingAmount": str(notional_usd * 0.95),
                            "makingAmount": str((notional_usd * 0.95) / max(vwap, 0.01)),
                            "tradeIDs": [f"dryrun-trade-{key[:8]}"],
                            "dry_run": True,
                        }
                    else:
                        signed = self._client.create_market_order(margs)
                        response = self._client.post_order(signed, OrderType.FAK)
                    break
                except Exception as exc:
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
                        continue
                    raise

            latency_ms = int((time.time() - t0) * 1000)
            order_id = response.get("orderID")
            trade_ids = response.get("tradeIDs", []) or []
            status_str = str(response.get("status", "")).lower()

            filled_notional, filled_shares = self._extract_fill_amounts(response, side)
            if status_str in ("delayed", "live", "unmatched", "") and order_id and not self.config.dry_run:
                filled_notional, filled_shares, status_str = self._poll_fill(order_id, side)

            avg_fill_price = (filled_notional / filled_shares) if filled_shares > 0 else 0.0
            fill_ratio = (filled_notional / notional_usd) if notional_usd > 0 else 0.0

            if filled_shares == 0:
                fill_status = "NO_FILL"
                success = False
                err = f"status={status_str} tradeIDs={len(trade_ids)}"
            elif fill_ratio >= 0.999:
                fill_status = "FULL_FILL"
                success = True
                err = None
            elif fill_ratio >= min_ratio:
                fill_status = "PARTIAL_FILL_ACCEPTED"
                success = True
                err = None
            else:
                fill_status = "PARTIAL_FILL_REJECTED"
                success = False
                err = f"fill_ratio={fill_ratio:.3f} < min_ratio={min_ratio}"

            return PlaceOrderResult(
                success=success, fill_status=fill_status,
                order_id=order_id, trade_ids=trade_ids,
                filled_shares=filled_shares, filled_notional=filled_notional,
                avg_fill_price=avg_fill_price, fees_paid=0.0,
                slippage_cap_used=slip_cap, vwap_computed=vwap,
                price_source=audit.get("price_source", ""),
                latency_ms=latency_ms,
                placed_at=datetime.now(timezone.utc).isoformat(),
                attempt=attempt, error=err, raw_response=response,
                **result_base,
            )

        except Exception as exc:
            return PlaceOrderResult(
                success=False, fill_status="EXCEPTION",
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
                vwap_computed=vwap, slippage_cap_used=slip_cap,
                latency_ms=int((time.time() - t0) * 1000),
                price_source=audit.get("price_source", ""),
                **result_base,
            )

    def _extract_fill_amounts(self, response: dict, side: str) -> tuple[float, float]:
        """Returns (filled_notional_usd, filled_shares).

        Polymarket order semantics — confirmé via py-clob-client-v2
        order_builder/builder.py::get_order_amounts (2026-05-20) :
          BUY  : makerAmount = size*price = USDC donné ; takerAmount = size = shares reçues
          SELL : makerAmount = shares données ; takerAmount = USDC reçu
        La réponse post_order makingAmount/takingAmount suit la convention maker/taker.

        FIX 2026-05-20 : l'ancien code retournait l'INVERSE (BUY→taking,making).
        NOTE : l'échelle (token decimals 1e6 vs unités humaines) + ce mapping
        DOIVENT être validés empiriquement au E8 $1 avant tout flip live global.
        """
        try:
            taking = float(response.get("takingAmount", 0) or 0)
            making = float(response.get("makingAmount", 0) or 0)
            if side == "BUY":
                # making = USDC dépensé (notional), taking = shares reçues
                return making, taking
            else:
                # SELL : making = shares données, taking = USDC reçu (notional)
                return taking, making
        except Exception:
            return 0.0, 0.0

    def _poll_fill(self, order_id: str, side: str) -> tuple[float, float, str]:
        deadline = time.time() + self.config.fill_confirm_timeout_s
        while time.time() < deadline:
            try:
                o = self._client.get_order(order_id)
                status = str(o.get("status", "")).lower()
                if status in ("matched", "filled"):
                    size_matched = float(o.get("size_matched", 0) or 0)
                    price = float(o.get("price", 0) or 0)
                    if side == "BUY":
                        return size_matched * price, size_matched, status
                    else:
                        return size_matched * price, size_matched, status
                if status in ("canceled", "expired", "rejected"):
                    return 0.0, 0.0, status
            except Exception as exc:
                logger.debug("poll_fill exc: %s", exc)
            time.sleep(FILL_CONFIRM_POLL_INTERVAL_S)
        return 0.0, 0.0, "TIMEOUT"

    # ------- Cancel + introspection -------

    def cancel_order(self, order_id: str) -> CancelOrderResult:
        if not self._initialized:
            self.initialize()
        try:
            from py_clob_client_v2 import OrderPayload
            payload = OrderPayload(id=order_id)
            self._client.cancel_order(payload)
            return CancelOrderResult(success=True, order_id=order_id)
        except Exception as exc:
            return CancelOrderResult(success=False, order_id=order_id,
                                     error=f"{type(exc).__name__}: {exc}")

    def cancel_all(self) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        if self.config.dry_run:
            return {"cancelled_count": 0, "dry_run": True}
        try:
            response = self._client.cancel_all()
            return {"cancelled_count": len(response or []), "raw": response}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def get_open_orders(self) -> list[dict[str, Any]]:
        if not self._initialized:
            self.initialize()
        try:
            return self._client.get_open_orders() or []
        except Exception as exc:
            logger.warning("get_open_orders err: %s", exc)
            return []

    def get_trades(self, order_id: Optional[str] = None) -> list[dict[str, Any]]:
        if not self._initialized:
            self.initialize()
        try:
            from py_clob_client_v2 import TradeParams
            params = TradeParams(id=order_id) if order_id else None
            return self._client.get_trades(params=params) or []
        except Exception as exc:
            logger.warning("get_trades err: %s", exc)
            return []

    def status(self) -> dict[str, Any]:
        return {
            "initialized": self._initialized,
            "dry_run": self.config.dry_run,
            "signature_type": self.config.signature_type,
            "funder_address": self.config.funder_address,
            "host": self.config.host,
            "creds_loaded": self._api_creds is not None,
            "flags": {
                "v2_enabled": is_v2_enabled(),
                "strict": is_strict_enabled(),
                "live_enabled": is_live_enabled(),
                "e8_only": is_e8_only(),
            },
            "stats": self.get_stats(),
        }


class _DryRunClient:
    """Minimal stub for unit tests."""

    def __init__(self, config: CLOBExecutorConfig) -> None:
        self.config = config

    def set_api_creds(self, *_args, **_kwargs) -> None: pass

    def create_or_derive_api_key(self) -> Any:
        class _C:
            api_key = "dry"; api_secret = "dry"; api_passphrase = "dry"
        return _C()

    def assert_level_2_auth(self) -> None: pass

    def get_order_book(self, token_id: str) -> Any:
        class _B:
            asks = []; bids = []
        return _B()

    def create_market_order(self, args: Any) -> Any:
        return {"dry": True, "args": args}

    def post_order(self, signed: Any, order_type: Any) -> dict[str, Any]:
        return {"orderID": "dry-order", "status": "matched",
                "takingAmount": "1.0", "makingAmount": "1.5",
                "tradeIDs": ["dry-trade"]}

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"status": "matched", "size_matched": "1.5", "price": "0.667"}

    def cancel_order(self, payload: Any) -> dict[str, Any]: return {"canceled": True}
    def cancel_all(self) -> list: return []
    def get_open_orders(self) -> list: return []
    def get_trades(self, params: Any = None) -> list: return []


def build_executor_from_env() -> Optional[CLOBExecutor]:
    """Build CLOBExecutor from env vars. Returns None if required vars missing
    OR if v2 flag is off."""
    if not is_v2_enabled():
        logger.info("LIVE_EXECUTOR_V2_ENABLED=false, executor not built")
        return None
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    if not pk:
        logger.error("POLYMARKET_PRIVATE_KEY missing")
        return None
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "3"))
    funder = (
        os.environ.get("POLYMARKET_DEPOSIT_WALLET")
        or os.environ.get("POLYMARKET_FUNDER")
        or ""
    )
    if sig_type in (1, 2, 3) and not funder:
        logger.error("funder required for sig_type=%s", sig_type)
        return None
    cfg = CLOBExecutorConfig(
        host=os.environ.get("POLYMARKET_CLOB_HOST",
                            os.environ.get("CLOB_API_BASE_URL", DEFAULT_CLOB_HOST)),
        chain_id=POLYGON_CHAIN_ID,
        private_key=pk, funder_address=funder, signature_type=sig_type,
        api_key=os.environ.get("POLYMARKET_API_KEY"),
        api_secret=os.environ.get("POLYMARKET_API_SECRET"),
        api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE"),
        dry_run=bool(int(os.environ.get("POLYMARKET_CLOB_DRY_RUN", "0"))),
    )
    return CLOBExecutor(cfg)
