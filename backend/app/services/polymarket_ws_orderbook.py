"""Polymarket WebSocket orderbook service — push sub-100ms.

DOCTRINE :
- Service séparé (pas dans clob_executor) pour séparation des responsabilités
- asyncio + websockets standard Python (pas de dep externe)
- Subscribe dynamique : top N tokens des markets actifs polled
- Cache in-memory : dict[token_id, LocalOrderbook]
- API simple : get_cached_orderbook(token_id) → OrderbookState | None
- PING toutes les 10s, auto-reconnect avec backoff exponentiel
- Si disconnect → re-fetch REST snapshot + resubscribe (pas de seq numbers)

USAGE :
    from app.services.polymarket_ws_orderbook import (
        WSOrderbookService, get_orderbook_service, get_cached_orderbook,
    )
    # Au boot main.py:
    svc = WSOrderbookService.from_env()
    await svc.start()
    # Runtime:
    book = get_cached_orderbook(token_id)
    if book and book.is_fresh(max_age_s=2.0):
        best_ask = book.best_ask()
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_REST_BOOK_URL = "https://clob.polymarket.com/book"
PING_INTERVAL_S = 10.0
MAX_RECONNECT_BACKOFF_S = 60.0
INITIAL_RECONNECT_BACKOFF_S = 1.0
DEFAULT_MAX_TOKENS = 500
DEFAULT_FRESHNESS_THRESHOLD_S = 2.0
# Éviction TTL : un token non ré-abonné depuis evict_ttl_s est lâché pour
# libérer un slot. Indispensable sous le churn crypto-5min (marchés neufs en
# continu) — sinon le cap max_tokens se remplit et bloque tout nouvel abonnement
# (le service n'a aucune éviction auto, _subscribed ne faisait que croître).
DEFAULT_EVICT_TTL_S = 1800.0
EVICT_SWEEP_INTERVAL_S = 60.0

# Flag for v2 live execution rollout
WS_ENABLED_FLAG = "POLYMARKET_WS_ORDERBOOK_ENABLED"


@dataclass
class OrderbookLevel:
    price: float
    size: float


@dataclass
class OrderbookState:
    """Local orderbook state for a single token, updated by WS events."""
    token_id: str
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    last_update_ts: float = 0.0
    last_trade_price: Optional[float] = None
    market_resolved: bool = False
    received_events: int = 0

    def apply_snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        """Replace orderbook state from a book event (snapshot)."""
        self.bids.clear()
        self.asks.clear()
        for b in bids or []:
            try:
                p = float(b.get("price"))
                s = float(b.get("size"))
                if s > 0:
                    self.bids[p] = s
            except (TypeError, ValueError):
                continue
        for a in asks or []:
            try:
                p = float(a.get("price"))
                s = float(a.get("size"))
                if s > 0:
                    self.asks[p] = s
            except (TypeError, ValueError):
                continue
        self.last_update_ts = time.time()
        self.received_events += 1

    def apply_price_change(self, changes: list[dict]) -> None:
        """Apply incremental price_change events."""
        for c in changes or []:
            try:
                price = float(c.get("price"))
                size = float(c.get("size"))
                side = (c.get("side") or "").upper()
            except (TypeError, ValueError):
                continue
            target = self.bids if side == "BUY" else self.asks
            if size == 0:
                target.pop(price, None)
            else:
                target[price] = size
        self.last_update_ts = time.time()
        self.received_events += 1

    def best_ask(self) -> Optional[float]:
        return min(self.asks.keys()) if self.asks else None

    def best_bid(self) -> Optional[float]:
        return max(self.bids.keys()) if self.bids else None

    def spread(self) -> Optional[float]:
        ba, bb = self.best_ask(), self.best_bid()
        if ba is None or bb is None:
            return None
        return ba - bb

    def is_fresh(self, max_age_s: float = DEFAULT_FRESHNESS_THRESHOLD_S) -> bool:
        if self.last_update_ts == 0:
            return False
        return (time.time() - self.last_update_ts) <= max_age_s

    def compute_vwap_buy(self, notional_usd: float) -> Optional[float]:
        """TRUE VWAP for a BUY consuming notional_usd of USDC.

        Walk asks cheapest-first, accumulate shares bought at each level.
        VWAP = notional / total_shares = the price a FAK market order actually
        averages at. NOT the worst price level (ancien bug 2026-05-20).
        Returns None if orderbook lacks depth.
        """
        if not self.asks:
            return None
        sorted_asks = sorted(self.asks.items())  # ascending price (cheapest first)
        remaining = notional_usd
        total_shares = 0.0
        for price, size in sorted_asks:
            if price <= 0:
                continue
            level_value = price * size  # USDC available at this level
            take = min(remaining, level_value)
            total_shares += take / price
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining > 1e-9 or total_shares <= 0:
            return None  # insufficient depth
        return notional_usd / total_shares

    def compute_worst_price_buy(self, notional_usd: float) -> Optional[float]:
        """Worst (highest) ask price level touched to fill notional_usd.
        Slippage-cap reference for FAK live orders (NOT the entry price)."""
        if not self.asks:
            return None
        total_value = 0.0
        for price, size in sorted(self.asks.items()):
            total_value += price * size
            if total_value >= notional_usd:
                return price
        return None

    def compute_vwap_sell(self, shares_amount: float) -> Optional[float]:
        """TRUE VWAP for a SELL of shares_amount.

        Walk bids highest-first, accumulate USDC received.
        VWAP = total_usdc / shares_amount. Returns None if depth insufficient."""
        if not self.bids:
            return None
        sorted_bids = sorted(self.bids.items(), reverse=True)  # descending price
        remaining = shares_amount
        total_usdc = 0.0
        for price, size in sorted_bids:
            take = min(remaining, size)
            total_usdc += take * price
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining > 1e-9 or shares_amount <= 0:
            return None  # insufficient depth
        return total_usdc / shares_amount

    def compute_worst_price_sell(self, shares_amount: float) -> Optional[float]:
        """Worst (lowest) bid price level touched. Slippage-cap ref for SELL."""
        if not self.bids:
            return None
        total_shares = 0.0
        for price, size in sorted(self.bids.items(), reverse=True):
            total_shares += size
            if total_shares >= shares_amount:
                return price
        return None

    def has_min_depth(self, side: str, notional_usd: float) -> bool:
        """Check if orderbook has enough depth to fill notional_usd."""
        if side == "BUY":
            return self.compute_vwap_buy(notional_usd) is not None
        else:
            return self.compute_vwap_sell(notional_usd) is not None


# Module-level singleton + module-level cache for easy access
_service_singleton: Optional["WSOrderbookService"] = None
_singleton_lock = threading.Lock()
_book_cache: dict[str, OrderbookState] = {}
_cache_lock = threading.Lock()


def get_cached_orderbook(token_id: str) -> Optional[OrderbookState]:
    """Thread-safe accessor for cached orderbook. Returns None if not subscribed/never received."""
    with _cache_lock:
        return _book_cache.get(token_id)


def get_orderbook_service() -> Optional["WSOrderbookService"]:
    return _service_singleton


def is_ws_enabled() -> bool:
    return os.environ.get(WS_ENABLED_FLAG, "0").strip() in ("1", "true", "True", "yes")


class WSOrderbookService:
    """Maintain a live WS subscription to Polymarket market channel for orderbook updates."""

    def __init__(
        self,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        custom_feature_enabled: bool = True,
    ) -> None:
        self.max_tokens = max_tokens
        self.custom_feature_enabled = custom_feature_enabled
        self._subscribed: set[str] = set()
        self._pending_subscribe: set[str] = set()
        self._pending_unsubscribe: set[str] = set()
        self._subscribed_at: dict[str, float] = {}
        self.evict_ttl_s = float(
            os.environ.get("POLYMARKET_WS_ORDERBOOK_TTL_S", DEFAULT_EVICT_TTL_S))
        self._last_evict_ts = 0.0
        self._running = False
        self._stopping = False
        self._ws: Any = None
        self._loop_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._stats = {
            "events_received": 0, "book_events": 0, "price_change_events": 0,
            "last_trade_events": 0, "market_resolved_events": 0,
            "reconnects": 0, "subscribe_calls": 0, "rest_snapshot_fetches": 0,
            "evictions": 0,
            "started_at": 0.0, "last_event_at": 0.0,
        }

    @classmethod
    def from_env(cls) -> "WSOrderbookService":
        return cls(
            max_tokens=int(os.environ.get("POLYMARKET_WS_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
            custom_feature_enabled=os.environ.get(
                "POLYMARKET_WS_CUSTOM_FEATURE", "1").strip() in ("1", "true", "True"),
        )

    async def start(self) -> None:
        """Launch the WS connection loop as a background asyncio task."""
        global _service_singleton
        with _singleton_lock:
            if _service_singleton is not None:
                logger.warning("WSOrderbookService already started")
                return
            _service_singleton = self
        if not is_ws_enabled():
            logger.info("WSOrderbookService disabled by flag %s", WS_ENABLED_FLAG)
            return
        self._running = True
        self._stats["started_at"] = time.time()
        self._loop_task = asyncio.create_task(self._connection_loop())
        logger.info("WSOrderbookService started (max_tokens=%d)", self.max_tokens)

    async def stop(self) -> None:
        global _service_singleton
        self._stopping = True
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        with _singleton_lock:
            _service_singleton = None
        logger.info("WSOrderbookService stopped")

    def subscribe(self, token_ids: list[str]) -> None:
        """Add token_ids to subscription. Idempotent. Applied at next loop iter.

        Ré-abonner un token déjà suivi rafraîchit son TTL (keepalive) : un marché
        activement copié reste abonné, un marché mort est évincé par
        _evict_stale. C'est ce qui permet l'alimentation continue par le service
        WS activity sans jamais saturer le cap max_tokens."""
        now = time.time()
        for t in token_ids:
            if not t:
                continue
            if t in self._subscribed or t in self._pending_subscribe:
                self._subscribed_at[t] = now  # keepalive TTL
            elif len(self._subscribed) + len(self._pending_subscribe) < self.max_tokens:
                self._pending_subscribe.add(t)
                self._subscribed_at[t] = now

    def unsubscribe(self, token_ids: list[str]) -> None:
        for t in token_ids:
            if t in self._subscribed:
                self._pending_unsubscribe.add(t)

    def _evict_stale(self) -> None:
        """Évince les tokens non ré-abonnés depuis evict_ttl_s. Libère des slots
        sous le cap max_tokens — sans ça le churn crypto-5min sature
        l'abonnement en quelques heures et bloque tout nouveau marché."""
        now = time.time()
        dead = [
            t for t, ts in list(self._subscribed_at.items())
            if t in self._subscribed and (now - ts) > self.evict_ttl_s
        ]
        for t in dead:
            self._pending_unsubscribe.add(t)
            self._subscribed_at.pop(t, None)
        if dead:
            self._stats["evictions"] += len(dead)
            logger.info("WSOrderbook: évincé %d tokens TTL-stale (subscribed=%d)",
                        len(dead), len(self._subscribed) - len(dead))

    def get_stats(self) -> dict[str, Any]:
        with _cache_lock:
            cache_size = len(_book_cache)
            fresh_count = sum(1 for b in _book_cache.values() if b.is_fresh())
        return {
            **self._stats,
            "running": self._running,
            "subscribed_count": len(self._subscribed),
            "pending_subscribe": len(self._pending_subscribe),
            "evict_ttl_s": self.evict_ttl_s,
            "cache_size": cache_size,
            "fresh_count": fresh_count,
            "uptime_s": time.time() - self._stats["started_at"] if self._stats["started_at"] else 0,
        }

    async def _connection_loop(self) -> None:
        """Main loop with reconnect backoff."""
        backoff = INITIAL_RECONNECT_BACKOFF_S
        while self._running and not self._stopping:
            try:
                await self._run_once()
                backoff = INITIAL_RECONNECT_BACKOFF_S  # reset on clean exit
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("WS connection error: %s: %s", type(exc).__name__, exc)
                self._stats["reconnects"] += 1
                # Re-fetch REST snapshot for all subscribed before resubscribe
                await self._refetch_all_snapshots()
            if self._stopping:
                break
            logger.info("WS reconnect in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF_S)

    async def _run_once(self) -> None:
        """Single WS session : connect, subscribe, listen until disconnect."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed. pip install websockets")
            raise

        async with websockets.connect(WS_MARKET_URL, ping_interval=None) as ws:
            self._ws = ws
            logger.info("WS connected to %s", WS_MARKET_URL)
            # Initial subscribe (all pending)
            if self._pending_subscribe:
                await self._send_subscribe(ws, list(self._pending_subscribe))
                self._subscribed.update(self._pending_subscribe)
                self._pending_subscribe.clear()
            # Start ping task
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw_msg in ws:
                    await self._handle_message(raw_msg)
                    # Apply pending sub/unsub
                    if self._pending_subscribe:
                        new_tokens = list(self._pending_subscribe)
                        await self._send_subscribe(ws, new_tokens)
                        self._subscribed.update(new_tokens)
                        self._pending_subscribe.clear()
                    if self._pending_unsubscribe:
                        # Polymarket WS doesn't have explicit unsubscribe op for market channel,
                        # we just remove from cache + subscribed set. Server keeps pushing but
                        # we drop messages locally.
                        for t in self._pending_unsubscribe:
                            self._subscribed.discard(t)
                            self._subscribed_at.pop(t, None)
                            with _cache_lock:
                                _book_cache.pop(t, None)
                        self._pending_unsubscribe.clear()
                    # Éviction TTL périodique — libère les slots des marchés morts.
                    now_evict = time.time()
                    if now_evict - self._last_evict_ts > EVICT_SWEEP_INTERVAL_S:
                        self._last_evict_ts = now_evict
                        self._evict_stale()
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _send_subscribe(self, ws: Any, token_ids: list[str]) -> None:
        msg = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": self.custom_feature_enabled,
        }
        await ws.send(json.dumps(msg))
        self._stats["subscribe_calls"] += 1
        logger.debug("WS subscribed %d tokens", len(token_ids))

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_S)
                try:
                    await ws.send("PING")
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, raw_msg: Any) -> None:
        if isinstance(raw_msg, bytes):
            raw_msg = raw_msg.decode("utf-8", errors="ignore")
        if raw_msg == "PONG" or raw_msg.strip() == "":
            return
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            return
        # Polymarket WS sends list of events or single event
        events = data if isinstance(data, list) else [data]
        for ev in events:
            try:
                await self._process_event(ev)
            except Exception as exc:
                logger.debug("event process err: %s", exc)
        self._stats["events_received"] += len(events)
        self._stats["last_event_at"] = time.time()

    async def _process_event(self, ev: dict) -> None:
        # Polymarket events don't have an explicit "event_type" field.
        # Inferred from presence of fields :
        #   - bids/asks → book snapshot for asset_id
        #   - price_changes (array, each with asset_id) → delta updates per token
        #   - last_trade_price → last fill price update
        if not isinstance(ev, dict):
            return

        # Case 1: book snapshot
        if "asset_id" in ev and ("bids" in ev or "asks" in ev):
            asset_id = str(ev.get("asset_id") or "")
            if not asset_id:
                return
            with _cache_lock:
                state = _book_cache.setdefault(asset_id, OrderbookState(token_id=asset_id))
            state.apply_snapshot(ev.get("bids", []), ev.get("asks", []))
            self._stats["book_events"] += 1
            return

        # Case 2: price_changes array (delta updates, can affect multiple tokens)
        if "price_changes" in ev and isinstance(ev["price_changes"], list):
            # Group changes by asset_id
            by_asset: dict[str, list[dict]] = {}
            for ch in ev["price_changes"]:
                aid = str(ch.get("asset_id") or "")
                if aid:
                    by_asset.setdefault(aid, []).append(ch)
            for aid, changes in by_asset.items():
                with _cache_lock:
                    state = _book_cache.setdefault(aid, OrderbookState(token_id=aid))
                state.apply_price_change(changes)
            self._stats["price_change_events"] += 1
            return

        # Case 3: last_trade_price event (single token)
        if "last_trade_price" in ev or ("price" in ev and "asset_id" in ev and "side" not in ev):
            asset_id = str(ev.get("asset_id") or "")
            if not asset_id:
                return
            with _cache_lock:
                state = _book_cache.setdefault(asset_id, OrderbookState(token_id=asset_id))
            try:
                price = ev.get("last_trade_price") or ev.get("price")
                state.last_trade_price = float(price)
            except (TypeError, ValueError):
                pass
            self._stats["last_trade_events"] += 1
            return

        # Case 4: market_resolved or other custom event
        if ev.get("event_type") == "market_resolved" or "resolution" in ev:
            asset_id = str(ev.get("asset_id") or ev.get("market") or "")
            if asset_id:
                with _cache_lock:
                    state = _book_cache.setdefault(asset_id, OrderbookState(token_id=asset_id))
                state.market_resolved = True
                self._stats["market_resolved_events"] += 1

    async def _refetch_all_snapshots(self) -> None:
        """On reconnect, re-fetch REST /book for all subscribed tokens."""
        try:
            import httpx
        except ImportError:
            return
        async with httpx.AsyncClient(timeout=5.0) as client:
            for token in list(self._subscribed):
                try:
                    r = await client.get(CLOB_REST_BOOK_URL, params={"token_id": token})
                    if r.status_code == 200:
                        d = r.json()
                        with _cache_lock:
                            state = _book_cache.setdefault(token, OrderbookState(token_id=token))
                        state.apply_snapshot(d.get("bids", []), d.get("asks", []))
                        self._stats["rest_snapshot_fetches"] += 1
                except Exception:
                    pass
