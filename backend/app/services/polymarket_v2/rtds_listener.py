"""RTDS WS listener — sub-100ms detection wallet activity Polymarket.

Endpoint : wss://ws-live-data.polymarket.com
Subscribe : {"action":"subscribe","subscriptions":[{"topic":"activity","type":"*"}]}

DIFFÉRENCES vs legacy polymarket_ws_activity.py (586 LOC) :
  - Pas de subscribe orderbook (séparé Phase B via polymarket_ws_orderbook.py)
  - Pas de worker queue (callback direct dans le receive loop = -10ms latence)
  - Pas de dispatch state / cluster logic (déplacé dans LeanGates layer)
  - Mesure de latence explicite (trade.timestamp vs event arrival)
  - Filter cohort = set lookup O(1), pas DB query par trade

Garde du legacy :
  - URL + subscribe payload (validés en prod)
  - Watchdog data-silence → reconnect (hérité [[feedback_long_run_self_healing]])
  - Reconnect avec backoff exponentiel

Push à callback : LatencyMeasuredEvent(trade_dict, latency_ms_event, latency_ms_total)
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import websockets


WS_URL = "wss://ws-live-data.polymarket.com"
SUBSCRIBE_PAYLOAD = {
    "action": "subscribe",
    "subscriptions": [{"topic": "activity", "type": "*"}],
}

PING_INTERVAL_S = 20.0
STALENESS_TIMEOUT_S = 90.0
RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 30.0


@dataclass
class RtdsEvent:
    """Event poussé au callback. Contient le trade brut + métriques latence."""
    trade: dict                 # payload brut Polymarket (asset, price, side, etc.)
    wallet: str                 # source wallet (lowercase, from "user" field)
    ts_event_arrival_ms: int    # quand le WS message est reçu chez nous
    ts_trade_ms: int            # timestamp dans le trade lui-même (source-of-truth)
    latency_arrival_ms: int     # = ts_event_arrival_ms - ts_trade_ms (peut être négatif si clock skew)


class RtdsListener:
    """WS listener pour activity stream Polymarket. Lean / fast / mesurable."""

    def __init__(
        self,
        cohort_wallets: set[str],
        on_event: Callable[[RtdsEvent], Awaitable[None]],
        ws_url: str = WS_URL,
        ping_interval_s: float = PING_INTERVAL_S,
        staleness_timeout_s: float = STALENESS_TIMEOUT_S,
        verbose: bool = True,
    ):
        # Cohort lookup O(1) — lowercase set
        self.cohort = {w.lower() for w in cohort_wallets}
        self.on_event = on_event
        self.ws_url = ws_url
        self.ping_interval_s = ping_interval_s
        self.staleness_timeout_s = staleness_timeout_s
        self.verbose = verbose

        self._stop = asyncio.Event()
        self._last_msg_ts: float = 0.0
        self._reconnect_attempt = 0

        # Stats observabilité (lecture périodique launcher)
        self.stats = {
            "ws_connects": 0,
            "ws_reconnects": 0,
            "msgs_received": 0,
            "msgs_decode_fail": 0,
            "trades_seen_total": 0,
            "trades_in_cohort": 0,
            "trades_out_cohort": 0,
            "callback_fail": 0,
            "latency_arrival_ms_p50": 0,
            "latency_arrival_ms_p99": 0,
        }
        # Rolling buffer pour percentiles (dernières 200 trades cohort)
        self._latency_buffer: list[int] = []
        self._latency_buffer_max = 200

    def _log(self, msg: str):
        if self.verbose:
            print(f"[RTDS] {msg}", flush=True)

    async def run(self):
        """Boucle principale : connect + reconnect avec backoff."""
        self._log(f"start, cohort={len(self.cohort)} wallets")
        while not self._stop.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"loop exception: {type(e).__name__}: {str(e)[:100]}")
            if self._stop.is_set():
                break
            # Backoff exponentiel
            self._reconnect_attempt += 1
            wait_s = min(RECONNECT_BASE_S * (2 ** (self._reconnect_attempt - 1)),
                         RECONNECT_MAX_S)
            self._log(f"reconnecting in {wait_s:.1f}s (attempt {self._reconnect_attempt})")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass

    async def _run_once(self):
        """Une session WS : connect, subscribe, receive jusqu'à erreur."""
        async with websockets.connect(self.ws_url, ping_interval=None) as ws:
            self.stats["ws_connects"] += 1
            if self._reconnect_attempt > 0:
                self.stats["ws_reconnects"] += 1
            self._reconnect_attempt = 0  # reset on successful connect
            self._last_msg_ts = time.time()
            self._log(f"connected to {self.ws_url}")

            await ws.send(json.dumps(SUBSCRIBE_PAYLOAD))
            self._log(f"subscribed: {SUBSCRIBE_PAYLOAD}")

            # Lance ping + watchdog en parallèle
            ping_task = asyncio.create_task(self._ping_loop(ws))
            watchdog_task = asyncio.create_task(self._watchdog_loop(ws))

            try:
                async for raw in ws:
                    self._last_msg_ts = time.time()
                    self.stats["msgs_received"] += 1
                    await self._handle_message(raw)
            finally:
                ping_task.cancel()
                watchdog_task.cancel()

    async def _ping_loop(self, ws):
        """Ping périodique pour garder la connexion alive."""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.ping_interval_s)
                await ws.send("PING")
            except (asyncio.CancelledError, websockets.ConnectionClosed):
                break
            except Exception:
                break

    async def _watchdog_loop(self, ws):
        """Si pas de message depuis staleness_timeout_s → close (déclenche reconnect)."""
        while not self._stop.is_set():
            await asyncio.sleep(10)
            silence = time.time() - self._last_msg_ts
            if silence > self.staleness_timeout_s:
                self._log(f"data silence {silence:.0f}s > {self.staleness_timeout_s}s → close WS")
                try:
                    await ws.close()
                except Exception:
                    pass
                break

    async def _handle_message(self, raw: str | bytes):
        """Parse le message + dispatch si trade cohort."""
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.stats["msgs_decode_fail"] += 1
            return

        # Polymarket RTDS envoie soit un trade unique, soit une liste, soit un
        # event control (subscribe-ack, pong). On filtre defensively.
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            await self._maybe_dispatch_trade(ev)

    async def _maybe_dispatch_trade(self, obj: dict):
        """Si c'est un trade activity et la wallet est cohort, push à callback.

        Format Polymarket RTDS confirmé via legacy polymarket_ws_activity.py :
          {"topic":"activity", "type":"trades"|"orders_matched",
           "payload":{proxyWallet, conditionId, slug, asset, side, price, size,
                       timestamp, ...}}
        """
        if obj.get("topic") != "activity":
            return
        # Garde uniquement les types qui sont des trades exécutés.
        # "trades" = trade fill normal. "orders_matched" = match d'ordre user.
        msg_type = obj.get("type")
        if msg_type not in ("trades", "orders_matched"):
            return
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return
        wallet = (payload.get("proxyWallet") or "").lower()
        if not wallet:
            return
        self.stats["trades_seen_total"] += 1
        if wallet not in self.cohort:
            self.stats["trades_out_cohort"] += 1
            return
        self.stats["trades_in_cohort"] += 1

        # Timestamp source du trade : on PRÉFÈRE le top-level `timestamp` (ms,
        # précision pleine, mesuré côté Polymarket WS server au push).
        # Le `payload.timestamp` est tronqué à la SECONDE → ajoute jusqu'à 1000ms
        # d'artefact si utilisé comme baseline latence.
        ts_trade_raw = obj.get("timestamp") or payload.get("timestamp") or payload.get("ts") or 0
        try:
            ts_trade_raw = int(ts_trade_raw)
        except (TypeError, ValueError):
            ts_trade_raw = 0
        # Détection auto sec vs ms : si < 1e12 alors secondes
        if ts_trade_raw > 0 and ts_trade_raw < 1_000_000_000_000:
            ts_trade_ms = ts_trade_raw * 1000
        else:
            ts_trade_ms = ts_trade_raw

        ts_arrival_ms = int(time.time() * 1000)
        latency = (ts_arrival_ms - ts_trade_ms) if ts_trade_ms > 0 else -1

        # Rolling latency buffer pour percentiles (seulement positifs et bornés)
        if 0 <= latency < 60_000:
            self._latency_buffer.append(latency)
            if len(self._latency_buffer) > self._latency_buffer_max:
                self._latency_buffer.pop(0)
            sorted_buf = sorted(self._latency_buffer)
            self.stats["latency_arrival_ms_p50"] = sorted_buf[len(sorted_buf) // 2]
            self.stats["latency_arrival_ms_p99"] = sorted_buf[int(len(sorted_buf) * 0.99)]

        # RtdsEvent.trade contient le inner payload (champs asset/side/price/etc.)
        # — pas l'enveloppe topic/type, qui est interne au transport.
        event = RtdsEvent(
            trade=payload, wallet=wallet,
            ts_event_arrival_ms=ts_arrival_ms,
            ts_trade_ms=ts_trade_ms,
            latency_arrival_ms=latency,
        )
        try:
            await self.on_event(event)
        except Exception as e:
            self.stats["callback_fail"] += 1
            self._log(f"callback failure: {type(e).__name__}: {str(e)[:100]}")

    def stop(self):
        self._stop.set()


# ─── CLI smoke test ──────────────────────────────────────────────────
if __name__ == "__main__":
    """Smoke : connect, log les 30 premiers trades cohort avec latency."""
    import sqlite3
    import sys

    DB = "/opt/app/polyoracle/data/polyoracle.db"

    async def main():
        # Load cohort
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT address FROM marketfirstwalletrecord WHERE candidate_status='ELITE'"
        ).fetchall()
        conn.close()
        cohort = {r[0].lower() for r in rows}
        print(f"[SMOKE] cohort loaded: {len(cohort)} ELITE wallets")

        n_seen = 0
        n_target = 30

        async def on_event(ev: RtdsEvent):
            nonlocal n_seen
            n_seen += 1
            trade = ev.trade
            print(f"[SMOKE #{n_seen}] {ev.wallet[:14]} "
                  f"{trade.get('side','?')} "
                  f"asset={str(trade.get('asset',''))[:10]} "
                  f"price={trade.get('price','?')} "
                  f"size={trade.get('size','?')} "
                  f"latency={ev.latency_arrival_ms}ms")
            if n_seen >= n_target:
                listener.stop()

        listener = RtdsListener(cohort_wallets=cohort, on_event=on_event)
        try:
            await asyncio.wait_for(listener.run(), timeout=600)
        except asyncio.TimeoutError:
            print("[SMOKE] timeout 10min — stop")
            listener.stop()
        print(f"\n[SMOKE] DONE. Stats: {listener.stats}")

    asyncio.run(main())
