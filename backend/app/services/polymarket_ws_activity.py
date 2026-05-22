"""Polymarket RTDS activity stream — détection de trades par WebSocket (2026-05-21).

REFONTE WS : remplace la latence du cycle de polling par une détection PUSH.
La cohorte refonte (~3 500 wallets) donne un cycle de polling de plusieurs
minutes → les marchés crypto 5min sont ratés. Le topic `activity` du RTDS
pousse CHAQUE trade Polymarket en sub-seconde, indépendamment de la taille de
la cohorte (une seule souscription globale).

Endpoint + protocole vérifiés le 2026-05-21 (real-time-data-client + probe live) :
  - URL       : wss://ws-live-data.polymarket.com
  - subscribe : {"action":"subscribe","subscriptions":[{"topic":"activity","type":"*"}]}
  - keepalive : envoyer la string "ping" toutes les 5s
  - message   : {"topic":"activity","type":"trades"|"orders_matched",
                 "payload":{proxyWallet,conditionId,slug,eventSlug,outcome,
                            outcomeIndex,price,side,size,timestamp(s),asset,
                            transactionHash,title,...},
                 "timestamp":<ms>,"connection_id":...}

Le `payload` d'un event type=trades a la MÊME forme que la réponse data-api
/trades → on réutilise tel quel `_raw_to_audit_input` + `_process_trade_in_session`.

Doctrine (identique à stream_pull_service) :
- Zéro changement audit / gates / cohorte / tiers / sizing / risk_engine.
- Nouveau service = lane de détection plus rapide, rien d'autre.
- Coexiste avec stream_pull / per-wallet polling (ADD coverage, never REMOVE).
- Feature flag POLYMARKET_WS_ACTIVITY_ENABLED (défaut False) pour rollback net.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:  # observabilité non critique — ne doit jamais casser le service
    from app.services import polling_delay_observer as _pdo
except Exception:  # pragma: no cover
    _pdo = None

try:  # WS orderbook — alimenté en continu par les trades détectés ci-dessous
    from app.services.polymarket_ws_orderbook import (
        get_orderbook_service as _get_ob_service,
        is_ws_enabled as _ob_ws_enabled,
        get_cached_orderbook as _get_cached_ob,
    )
except Exception:  # pragma: no cover
    _get_ob_service = None
    _ob_ws_enabled = None
    _get_cached_ob = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


WS_URL = "wss://ws-live-data.polymarket.com"
WS_ENABLED_FLAG = "POLYMARKET_WS_ACTIVITY_ENABLED"
SUBSCRIBE_MSG = {
    "action": "subscribe",
    "subscriptions": [{"topic": "activity", "type": "*"}],
}
PING_INTERVAL_S = 5.0
INITIAL_RECONNECT_BACKOFF_S = 1.0
MAX_RECONNECT_BACKOFF_S = 30.0
DEDUP_LRU_SIZE = 20_000
QUEUE_MAXSIZE = 4000
COHORT_REFRESH_S = 60.0
# Même règle que stream_pull : au-delà, le pipeline rejette en
# STALE_SIGNAL_BACKFILL. En push WS les trades sont frais — garde défensive.
DEFAULT_MAX_TRADE_AGE_S = _env_int("POLYMARKET_WS_MAX_TRADE_AGE_S", 90)
# Watchdog flux : le WS RTDS peut rester TCP-alive (ping/pong honorés) mais
# cesser de pousser des events activity — aucune exception n'est levée, le
# service reste 'connected' sans plus rien recevoir. Si aucun event pendant
# STALE_EVENT_THRESHOLD_S → reconnexion forcée (re-souscription propre).
STALE_EVENT_THRESHOLD_S = float(_env_int("POLYMARKET_WS_STALE_THRESHOLD_S", 90))
STALE_CHECK_INTERVAL_S = 30.0

# Rejected ledger : fichier SQLite SÉPARÉ (zéro contention avec polyoracle.db).
# Logge chaque rejet de gate strict pour mesurer a posteriori l'EV des trades
# qu'on ne prend pas (BAND_MISMATCH, LOW_LIQ, CLOB_NOT_FILLABLE, SELL...).
REJECT_LEDGER_PATH = os.environ.get(
    "STRICT_REJECT_LEDGER_PATH",
    "/opt/app/polyoracle/data/strict_reject_ledger.db")


def is_ws_activity_enabled() -> bool:
    return _env_bool(WS_ENABLED_FLAG, False)


class WSActivityService:
    """Souscrit au topic RTDS `activity`, filtre la cohorte, dispatche le pipeline."""

    def __init__(
        self,
        polling_engine: Any,
        *,
        max_trade_age_s: int = DEFAULT_MAX_TRADE_AGE_S,
    ) -> None:
        self.polling_engine = polling_engine
        self.max_trade_age_s = max_trade_age_s
        self._stop_event = asyncio.Event()
        self._conn_task: Optional[asyncio.Task] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._processed_ids: deque[str] = deque(maxlen=DEDUP_LRU_SIZE)
        self._processed_id_set: set[str] = set()
        self._cohort_set: Optional[set[str]] = None
        self._cohort_refreshed_at = 0.0
        self._started_at: Optional[float] = None
        # Compteurs (lus par /observability ou endpoint status).
        self.connects = 0
        self.reconnects = 0
        self.stale_reconnects = 0
        self.events_received = 0
        self.trade_events = 0
        self.orders_matched_events = 0
        self.trades_seen_total = 0
        self.trades_matched_cohort = 0
        self.trades_dedup_skipped = 0
        self.trades_skipped_too_old = 0
        self.last_skipped_too_old_age_s: Optional[int] = None
        self.trades_dispatched = 0
        self.paper_executed = 0
        self.queue_dropped = 0
        self.orderbook_subs = 0
        self.connected_since: Optional[float] = None
        self.last_event_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.reasons: dict[str, int] = {}
        self.reject_logged = 0
        self._reject_db: Any = None

    # ---- lifecycle -------------------------------------------------------
    def is_running(self) -> bool:
        return self._conn_task is not None and not self._conn_task.done()

    async def start(self) -> None:
        if not is_ws_activity_enabled():
            logger.info("ws_activity: désactivé par le flag %s", WS_ENABLED_FLAG)
            return
        if self.is_running():
            logger.warning("ws_activity: déjà démarré, skip")
            return
        self._stop_event.clear()
        self._started_at = time.time()
        self._init_reject_ledger()
        self._worker_task = asyncio.create_task(
            self._dispatch_worker(), name="ws_activity_worker")
        self._conn_task = asyncio.create_task(
            self._run_loop(), name="ws_activity_loop")
        logger.warning("ws_activity: démarré url=%s", WS_URL)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        for t in (self._conn_task, self._worker_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._conn_task = None
        self._worker_task = None
        if self._reject_db is not None:
            try:
                self._reject_db.close()
            except Exception:
                pass
            self._reject_db = None
        logger.warning("ws_activity: arrêté")

    # ---- connexion -------------------------------------------------------
    async def _run_loop(self) -> None:
        backoff = INITIAL_RECONNECT_BACKOFF_S
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                backoff = INITIAL_RECONNECT_BACKOFF_S  # reset après sortie propre
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.reconnects += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("ws_activity: erreur connexion : %s", self.last_error)
            if self._stop_event.is_set():
                break
            logger.info("ws_activity: reconnexion dans %.1fs", backoff)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF_S)

    async def _run_once(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("ws_activity: package websockets absent (pip install websockets)")
            raise
        async with websockets.connect(
            WS_URL, ping_interval=20, open_timeout=15
        ) as ws:
            self._ws = ws
            self.connects += 1
            self.connected_since = time.time()
            logger.warning("ws_activity: connecté à %s", WS_URL)
            await ws.send(json.dumps(SUBSCRIBE_MSG))
            # Baseline watchdog : évite un faux-positif juste après connexion.
            self.last_event_at = time.time()
            ping_task = asyncio.create_task(self._ping_loop(ws))
            watchdog_task = asyncio.create_task(self._staleness_watchdog(ws))
            try:
                async for raw in ws:
                    if self._stop_event.is_set():
                        break
                    self._handle_message(raw)
            finally:
                for _t in (ping_task, watchdog_task):
                    _t.cancel()
                    try:
                        await _t
                    except (asyncio.CancelledError, Exception):
                        pass
                self.connected_since = None
                self._ws = None

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_S)
                await ws.send("ping")
        except (asyncio.CancelledError, Exception):
            pass

    async def _staleness_watchdog(self, ws: Any) -> None:
        """Force la reconnexion si le flux activity est silencieux trop longtemps.

        Le WS RTDS peut rester TCP-alive (ping/pong honorés) tout en cessant de
        pousser des events — `websockets` ne lève alors aucune exception et le
        service reste 'connected' sans rien recevoir (mort silencieuse observée
        2026-05-21 : flux figé après ~12min, 0 reconnexion). On surveille
        last_event_at et on close le socket → _run_loop reconnecte + re-souscrit."""
        try:
            while True:
                await asyncio.sleep(STALE_CHECK_INTERVAL_S)
                if self.last_event_at is None:
                    continue
                silence = time.time() - self.last_event_at
                if silence > STALE_EVENT_THRESHOLD_S:
                    self.stale_reconnects += 1
                    self.last_error = (
                        f"STALE_STREAM: 0 event depuis {silence:.0f}s "
                        f"-> reconnexion forcee")
                    logger.warning("ws_activity: %s", self.last_error)
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
        except (asyncio.CancelledError, Exception):
            pass

    # ---- réception (rapide, sync — n'appelle aucun I/O DB) ---------------
    def _get_cohort_set(self) -> set[str]:
        now = time.monotonic()
        if self._cohort_set is None or (now - self._cohort_refreshed_at) > COHORT_REFRESH_S:
            cohort_list = getattr(self.polling_engine, "_cohort", None) or []
            self._cohort_set = {str(a).lower() for a in cohort_list}
            self._cohort_refreshed_at = now
        return self._cohort_set

    def _maybe_subscribe_orderbook(self, asset: Any) -> None:
        """Abonne le WS orderbook au token EXACT de ce trade cohorte.

        Le WS orderbook n'était alimenté qu'une fois au boot (snapshot SQL
        statique) — inutile pour les marchés crypto-5min qui tournent en
        continu : au moment où le bot trade, ces marchés sont résolus et les
        neufs ne sont jamais abonnés → get_cached_orderbook renvoie None →
        clob_executor retombe sur REST /book qui 404. Ici chaque trade détecté
        pousse son token_id vers le WS orderbook → book frais pour le pricing.
        Best-effort, idempotent, jamais bloquant. Le ré-abonnement rafraîchit le
        TTL côté orderbook (keepalive) ; un marché mort est évincé tout seul."""
        if _get_ob_service is None or not asset:
            return
        if _ob_ws_enabled is not None and not _ob_ws_enabled():
            return  # WS orderbook désactivé par flag → ne pas accumuler de pending
        try:
            svc = _get_ob_service()
            if svc is not None:
                svc.subscribe([str(asset)])
                self.orderbook_subs += 1
        except Exception:  # pragma: no cover — jamais casser la réception WS
            pass

    def _handle_message(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        raw = (raw or "").strip()
        if not raw or raw.lower() in ("ping", "pong"):
            return
        try:
            obj = json.loads(raw)
        except Exception:
            return
        self.events_received += 1
        self.last_event_at = time.time()
        if obj.get("topic") != "activity":
            return
        etype = obj.get("type")
        if etype == "orders_matched":
            self.orders_matched_events += 1
            return
        if etype != "trades":
            return
        self.trade_events += 1
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return
        self.trades_seen_total += 1
        wallet = (payload.get("proxyWallet") or "").lower()
        if not wallet or wallet not in self._get_cohort_set():
            return
        self.trades_matched_cohort += 1
        # Alimente le WS orderbook en continu : abonne le book du token que ce
        # wallet cohorte vient de trader (indépendant du dédup / tid ci-dessous).
        self._maybe_subscribe_orderbook(payload.get("asset"))
        tid = payload.get("transactionHash") or payload.get("id")
        if not tid:
            return
        if tid in self._processed_id_set:
            self.trades_dedup_skipped += 1
            return
        # Marque dédup à l'enqueue : capte les doublons encore en file.
        self._processed_ids.append(tid)
        self._processed_id_set.add(tid)
        if len(self._processed_id_set) > DEDUP_LRU_SIZE + 100:
            self._processed_id_set = set(self._processed_ids)
        try:
            self._queue.put_nowait((tid, wallet, payload))
        except asyncio.QueueFull:
            self.queue_dropped += 1
            logger.warning("ws_activity: file pleine, trade droppé tid=%s", str(tid)[:16])
        if self.trade_events % 500 == 0:
            logger.warning(
                "ws_activity: events=%d trades_seen=%d matched=%d dispatched=%d "
                "executed=%d queue=%d dropped=%d",
                self.events_received, self.trades_seen_total,
                self.trades_matched_cohort, self.trades_dispatched,
                self.paper_executed, self._queue.qsize(), self.queue_dropped,
            )

    # ---- dispatch (lent : I/O DB — sérialisé hors du socket) -------------
    async def _dispatch_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                tid, wallet, trade = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                await self._dispatch_one(tid, wallet, trade)
            except Exception as exc:
                logger.warning(
                    "ws_activity: dispatch échoué tid=%s wallet=%s err=%s",
                    str(tid)[:16], wallet[:10], exc)
            finally:
                self._queue.task_done()

    async def _dispatch_one(self, tid: str, wallet: str, trade: dict) -> None:
        # Filtre âge avant dispatch (cf. stream_pull patch 2026-05-16).
        trade_ts = int(trade.get("timestamp") or 0)
        now_ts = int(time.time())
        if trade_ts and (now_ts - trade_ts) > self.max_trade_age_s:
            self.trades_skipped_too_old += 1
            self.last_skipped_too_old_age_s = now_ts - trade_ts
            return
        # Normalizer + processor EXACTEMENT comme stream_pull / per-wallet polling.
        normalized = self.polling_engine._raw_to_audit_input(trade, wallet)
        _obs = _pdo.start_observation({**trade, "wallet_address": wallet}) if _pdo else None
        result = None
        try:
            result = await asyncio.to_thread(
                self.polling_engine._process_trade_in_session, normalized)
        finally:
            if _pdo is not None:
                _pdo.finalize_from_result(_obs, result)
        self.trades_dispatched += 1
        try:
            self.polling_engine._trades_detected_session += 1
        except Exception:
            pass
        executed = bool(result and result.get("executed"))
        if executed:
            self.paper_executed += 1
            try:
                self.polling_engine._paper_trades_session += 1
            except Exception:
                pass
        else:
            reason = str((result or {}).get("reason") or "UNKNOWN")[:64]
            self.reasons[reason] = self.reasons.get(reason, 0) + 1
            self._log_reject(tid, wallet, trade, result)
        logger.warning(
            "ws_activity: dispatch wallet=%s slug=%s side=%s size=%s price=%s "
            "-> executed=%s reason=%s",
            wallet[:10], trade.get("slug"), trade.get("side"),
            trade.get("size"), trade.get("price"),
            executed, (result or {}).get("reason"),
        )

    # ---- rejected ledger -------------------------------------------------
    def _init_reject_ledger(self) -> None:
        """Ouvre/crée le ledger SQLite des rejets — fichier SÉPARÉ de la DB
        principale (aucune contention ni risque sur polyoracle.db). v1.1 :
        WAL, colonnes orderbook, clé unique anti-doublon, champs résolution."""
        try:
            import sqlite3
            db = sqlite3.connect(REJECT_LEDGER_PATH, timeout=10,
                                 check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS strict_reject ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, trade_ts INTEGER, "
                "tid TEXT, wallet TEXT, market_id TEXT, token_id TEXT, slug TEXT, "
                "outcome TEXT, side TEXT, source_price REAL, source_size REAL, "
                "band TEXT, reason TEXT, reason_code TEXT, copyable_edge REAL, "
                "our_vwap REAL, worst_price REAL, spread REAL, depth_ok INTEGER, "
                "orderbook_status TEXT, proposed_size REAL, wallet_tier TEXT, "
                "lane TEXT, would_have_won INTEGER, hyp_pnl_net REAL, detail TEXT)")
            # migration in-place si la table existait en v1 (ADD COLUMN idempotent)
            for col, typ in (("our_vwap", "REAL"), ("worst_price", "REAL"),
                             ("spread", "REAL"), ("depth_ok", "INTEGER"),
                             ("orderbook_status", "TEXT"), ("proposed_size", "REAL"),
                             ("wallet_tier", "TEXT"), ("lane", "TEXT"),
                             ("would_have_won", "INTEGER"), ("hyp_pnl_net", "REAL")):
                try:
                    db.execute("ALTER TABLE strict_reject ADD COLUMN %s %s" % (col, typ))
                except Exception:
                    pass
            db.execute("CREATE INDEX IF NOT EXISTS ix_rej_market ON strict_reject(market_id)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_rej_reason ON strict_reject(reason)")
            try:
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_rej_tid "
                           "ON strict_reject(tid, reason)")
            except Exception:  # doublons v1 préexistants — index non-unique toléré
                pass
            db.commit()
            self._reject_db = db
            logger.warning("ws_activity: rejected ledger v1.1 ouvert -> %s",
                            REJECT_LEDGER_PATH)
        except Exception as exc:  # pragma: no cover
            self._reject_db = None
            logger.warning("ws_activity: rejected ledger init échouée : %s", exc)

    def _log_reject(self, tid: Any, wallet: str, trade: dict, result: Any) -> None:
        """Logge un rejet de gate strict (ledger v1.1) : enrichi avec l'orderbook
        WS (VWAP/worst/spread/depth potentiels). Best-effort — ne casse jamais
        le dispatch. INSERT OR IGNORE = anti-doublon via clé unique (tid,reason)."""
        db = self._reject_db
        if db is None:
            return
        try:
            slug = trade.get("slug") or ""
            sl = slug.lower()
            band = ("5m" if ("updown-5m" in sl or "-5m-" in sl or "5min" in sl)
                    else "15m" if ("updown-15m" in sl or "-15m-" in sl or "15min" in sl)
                    else "60m" if ("60m" in sl or "hourly" in sl) else "?")
            res = result or {}
            side = (trade.get("side") or "BUY").upper()
            # enrichissement orderbook WS : notre VWAP/worst/spread/depth potentiels.
            our_vwap = worst_price = spread = None
            depth_ok = None
            ob_status = "NO_BOOK"
            try:
                if _get_cached_ob is not None:
                    ob = _get_cached_ob(str(trade.get("asset") or ""))
                    if ob is not None:
                        ob_status = "WS_FRESH" if ob.is_fresh(max_age_s=4.0) else "WS_STALE"
                        if side == "BUY":
                            our_vwap = ob.compute_vwap_buy(2.0)
                            worst_price = ob.compute_worst_price_buy(2.0)
                        else:
                            our_vwap = ob.compute_vwap_sell(2.0)
                            worst_price = ob.compute_worst_price_sell(2.0)
                        spread = ob.spread()
                        depth_ok = 1 if our_vwap is not None else 0
            except Exception:
                pass
            extra = {k: res[k] for k in (
                "profile", "liquidity_score", "clob_status", "price_source") if k in res}
            import datetime as _dt
            db.execute(
                "INSERT OR IGNORE INTO strict_reject (ts,trade_ts,tid,wallet,"
                "market_id,token_id,slug,outcome,side,source_price,source_size,"
                "band,reason,reason_code,copyable_edge,our_vwap,worst_price,spread,"
                "depth_ok,orderbook_status,proposed_size,wallet_tier,lane,detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_dt.datetime.now(_dt.timezone.utc).isoformat(),
                 int(trade.get("timestamp") or 0), str(tid), wallet,
                 trade.get("conditionId"), trade.get("asset"), slug,
                 trade.get("outcome"), side,
                 _safe_float(trade.get("price")), _safe_float(trade.get("size")),
                 band, str(res.get("reason") or "")[:80],
                 str(res.get("reason_code") or "")[:48],
                 _safe_float(res.get("copyable_edge")),
                 _safe_float(our_vwap), _safe_float(worst_price), _safe_float(spread),
                 depth_ok, ob_status, _safe_float(res.get("proposed_size_usd")),
                 res.get("wallet_tier"), res.get("lane"),
                 json.dumps(extra) if extra else None))
            db.commit()
            self.reject_logged += 1
        except Exception:  # pragma: no cover
            pass

    # ---- observabilité ---------------------------------------------------
    def get_stats(self) -> dict[str, Any]:
        return {
            "running": self.is_running(),
            "connected": self.connected_since is not None,
            "connects": self.connects,
            "reconnects": self.reconnects,
            "stale_reconnects": self.stale_reconnects,
            "uptime_s": (time.time() - self._started_at) if self._started_at else 0,
            "events_received": self.events_received,
            "trade_events": self.trade_events,
            "orders_matched_events": self.orders_matched_events,
            "trades_seen_total": self.trades_seen_total,
            "trades_matched_cohort": self.trades_matched_cohort,
            "trades_dedup_skipped": self.trades_dedup_skipped,
            "trades_skipped_too_old": self.trades_skipped_too_old,
            "last_skipped_too_old_age_s": self.last_skipped_too_old_age_s,
            "trades_dispatched": self.trades_dispatched,
            "paper_executed": self.paper_executed,
            "queue_size": self._queue.qsize(),
            "queue_dropped": self.queue_dropped,
            "orderbook_subs": self.orderbook_subs,
            "cohort_size": len(self._cohort_set or ()),
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "reject_logged": self.reject_logged,
            "reasons": dict(self.reasons),
        }


_INSTANCE: Optional[WSActivityService] = None


def get_ws_activity_service() -> Optional[WSActivityService]:
    return _INSTANCE


def init_ws_activity_service(polling_engine: Any, **kwargs) -> WSActivityService:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = WSActivityService(polling_engine, **kwargs)
    return _INSTANCE
