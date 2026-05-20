from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    bot,
    debug,
    discovery,
    edge,
    health,
    logs,
    markets,
    observability,
    paper,
    risk,
    settings as settings_routes,
    signals,
    storage,
    trades,
    vault,
    wallets,
)
from app.config import get_settings
from app.database import init_db

settings = get_settings()

app = FastAPI(
    title="POLYORACLE API",
    version="0.5.4",
    description="Smart-money audit bot for Polymarket. Local-first, observe massively, trade only the best signals.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    try:
        from app.services.wallet_polling_engine import WalletPollingEngine
        await WalletPollingEngine.maybe_resume()
    except PermissionError:
        # LIVE_ENABLED=true blocks polling — log and skip.
        pass
    except Exception:
        pass

    # Phase G HOTFIX (Round 8 review audit, 2026-05-13):
    # Auto-reclass daily — signature corrigée. run_weekly_reclass exige
    # db_path + backup_dir en kwargs. Pré-hotfix l'appel était `(session,
    # dry_run=False)` → TypeError silencieuse, cron tournait à vide.
    try:
        import asyncio
        import logging
        from pathlib import Path
        from app.services.weekly_reclass_service import run_weekly_reclass
        from app.database import engine
        from app.config import get_settings
        from sqlmodel import Session

        _settings = get_settings()
        # Resolve db_path from settings.database_url or fall back to canonical path
        _db_url = (_settings.database_url or "").replace("sqlite:///", "")
        _db_path = Path(_db_url) if _db_url else Path("/opt/app/polyoracle/data/polyoracle.db")
        _backup_dir = Path("/opt/app/polyoracle/data/_reclass_backups")
        _backup_dir.mkdir(parents=True, exist_ok=True)

        # HOTFIX 2026-05-13 14:00 UTC — disk leak postmortem:
        # Pre-fix this loop ran 1×/24h, but if systemd restarted the service
        # (watchdog timeout etc), each restart re-fired the loop = a new reclass
        # = a new 1.4G backup. With 132 restarts, this consumed 180 GB of disk
        # and crashed the VPS. Fix: a "last-run" stamp file under data/ that
        # the loop checks before reclassing. Skip if last run < 23h ago.
        _last_run_file = _db_path.parent / "_reclass_last_run.txt"

        def _hours_since_last_reclass() -> float:
            try:
                if not _last_run_file.exists():
                    return 999.0
                ts_str = _last_run_file.read_text().strip()
                last = datetime.fromisoformat(ts_str)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                delta = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                return delta
            except Exception:
                return 999.0

        def _stamp_last_reclass() -> None:
            try:
                _last_run_file.write_text(datetime.now(timezone.utc).isoformat())
            except Exception:
                pass

        from datetime import datetime, timezone

        async def _daily_reclass_loop():
            await asyncio.sleep(60)  # boot grace period
            while True:
                try:
                    # Skip reclass if one ran < 23h ago (= surviving a service
                    # restart should NOT re-trigger reclass + backup)
                    hours_ago = _hours_since_last_reclass()
                    if hours_ago < 23.0:
                        logging.getLogger(__name__).info(
                            "auto-reclass skipped: last run %.1fh ago (need ≥23h)",
                            hours_ago,
                        )
                    else:
                        with Session(engine) as session:
                            result = run_weekly_reclass(
                                session,
                                db_path=_db_path,
                                backup_dir=_backup_dir,
                                dry_run=False,
                            )
                        _stamp_last_reclass()
                        promoted = len(getattr(result, "promoted", []) or [])
                        demoted = len(getattr(result, "demoted", []) or [])
                        if promoted or demoted:
                            logging.getLogger(__name__).info(
                                "auto-reclass daily: +%d ELITE, -%d demoted",
                                promoted, demoted,
                            )
                            try:
                                engine_instance = WalletPollingEngine.instance()
                                engine_instance._cohort = []
                            except Exception:
                                pass
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "auto-reclass daily failed: %s: %s",
                        type(exc).__name__, exc,
                    )
                await asyncio.sleep(86400)

        asyncio.create_task(_daily_reclass_loop())
    except Exception as _bootexc:
        import logging
        logging.getLogger(__name__).warning(
            "auto-reclass daily failed to launch: %s: %s",
            type(_bootexc).__name__, _bootexc,
        )

    # v0.7.9 (2026-05-20) — Polymarket WebSocket orderbook subscription service.
    # Push sub-100ms orderbook updates for tokens the bot polls. Feeds the
    # clob_executor.get_executable_price() cache for live FAK orders.
    print("[BOOT] ws_orderbook startup hook entered", flush=True)
    try:
        from app.services.polymarket_ws_orderbook import (
            WSOrderbookService, is_ws_enabled,
        )
        print(f"[BOOT] ws import OK, is_ws_enabled()={is_ws_enabled()}", flush=True)
        if is_ws_enabled():
            ws_svc = WSOrderbookService.from_env()
            print(f"[BOOT] WSOrderbookService.from_env() OK max_tokens={ws_svc.max_tokens}", flush=True)
            await ws_svc.start()
            print(f"[BOOT] ws_svc.start() returned, singleton check...", flush=True)
            from app.services.polymarket_ws_orderbook import get_orderbook_service
            sing = get_orderbook_service()
            print(f"[BOOT] singleton after start: {sing}", flush=True)
            # Initial subscribe : tokens from currently-active markets
            try:
                from sqlmodel import Session, text
                from app.database import engine as _db_engine
                import json as _json
                with Session(_db_engine) as _s:
                    rows = _s.exec(text(
                        "SELECT clob_token_ids FROM market WHERE active=1 "
                        "AND deadline > datetime('now', '+5 minutes') "
                        "ORDER BY updated_at DESC LIMIT 200"
                    )).all()
                    tokens: list[str] = []
                    for (toks,) in rows:
                        try:
                            for t in _json.loads(toks):
                                tokens.append(str(t))
                        except Exception:
                            pass
                    if tokens:
                        ws_svc.subscribe(tokens[:400])
                        import logging
                        logging.getLogger(__name__).info(
                            "ws_orderbook initial subscribe: %d tokens", len(tokens[:400])
                        )
            except Exception as _wsboot:
                import logging
                logging.getLogger(__name__).warning(
                    "ws_orderbook initial subscribe failed: %s: %s",
                    type(_wsboot).__name__, _wsboot,
                )
    except Exception as _wsexc:
        import logging, traceback
        print(f"[BOOT] !! ws_orderbook FAIL: {type(_wsexc).__name__}: {_wsexc}", flush=True)
        traceback.print_exc()
        logging.getLogger(__name__).warning(
            "ws_orderbook service failed to launch: %s: %s",
            type(_wsexc).__name__, _wsexc,
        )

    # 2026-05-14 CLOB retry worker (Round 9 review strict-compatible fix).
    # Re-test orderbook BAD/UNTRADABLE candidates every ~5s; if CLOB book
    # becomes GOOD/ACCEPTABLE and every strict gate still passes, the trade
    # is opened (when clob_retry_auto_open_paper=True) or marked READY_TO_FILL
    # (phase 1 measure-only). Drill 2026-05-14 shows 42% recoverable.
    try:
        import asyncio
        import logging
        from sqlmodel import Session as _Session
        from app.database import engine as _eng
        from app.config import get_settings as _gs
        from app.services.clob_retry_service import ClobRetryService

        _s = _gs()
        if _s.clob_retry_enabled:
            async def _clob_retry_loop():
                await asyncio.sleep(60)  # boot grace
                interval = max(1, int(_s.clob_retry_worker_interval_seconds))
                _logger = logging.getLogger(__name__)
                while True:
                    try:
                        with _Session(_eng) as session:
                            svc = ClobRetryService(session)
                            counts = svc.process_pending_batch()
                        non_zero = {k: v for k, v in counts.items() if v > 0}
                        if non_zero:
                            _logger.info("clob_retry batch %s", non_zero)
                    except Exception as _exc:
                        _logger.warning(
                            "clob_retry loop iteration error %s: %s",
                            type(_exc).__name__, _exc,
                        )
                    await asyncio.sleep(interval)

            asyncio.create_task(_clob_retry_loop())
    except Exception as _bootexc2:
        import logging
        logging.getLogger(__name__).warning(
            "clob_retry worker failed to launch: %s: %s",
            type(_bootexc2).__name__, _bootexc2,
        )

    # Stream-pull service (2026-05-16) — batched /trades?limit=1000 polling
    # in parallel of per-wallet polling. Feature-flagged via
    # STREAM_PULL_ENABLED env. Doctrine : coexiste avec polling per-wallet
    # (= redondance / fallback). N'altère ni audit, ni gates, ni cohort.
    try:
        from app.services.stream_pull_service import (
            STREAM_PULL_ENABLED,
            init_stream_pull_service,
        )
        if STREAM_PULL_ENABLED:
            from app.services.wallet_polling_engine import WalletPollingEngine
            _engine = WalletPollingEngine.instance()
            _stream = init_stream_pull_service(_engine)
            await _stream.start()
            import logging
            logging.getLogger(__name__).info(
                "stream_pull: enabled via STREAM_PULL_ENABLED=true"
            )
        else:
            import logging
            logging.getLogger(__name__).info(
                "stream_pull: disabled (STREAM_PULL_ENABLED=false)"
            )
    except Exception as _bootexc3:
        import logging
        logging.getLogger(__name__).warning(
            "stream_pull worker failed to launch: %s: %s",
            type(_bootexc3).__name__, _bootexc3,
        )

    # P0.3 (2026-05-18) — FreshApiEnrichmentService SHADOW.
    # Measure-only : quantifies COLD lane angle mort by checking Polymarket
    # data-api for COLD wallets. Does NOT modify classifier. Feature-flagged
    # via FRESH_API_SHADOW_ENABLED. Doctrine : zéro changement comportemental.
    try:
        from app.services.fresh_api_enrichment_shadow import (
            run_loop_shadow, env_bool,
        )
        if env_bool("FRESH_API_SHADOW_ENABLED", False):
            import asyncio
            import logging
            asyncio.create_task(run_loop_shadow())
            logging.getLogger(__name__).info(
                "fresh_api_shadow: enabled via FRESH_API_SHADOW_ENABLED=true"
            )
    except Exception as _bootexc4:
        import logging
        logging.getLogger(__name__).warning(
            "fresh_api_shadow failed to launch: %s: %s",
            type(_bootexc4).__name__, _bootexc4,
        )


app.include_router(health.router)
app.include_router(markets.router)
app.include_router(wallets.router)
app.include_router(trades.router)
app.include_router(signals.router)
app.include_router(bot.router)
app.include_router(paper.router)
app.include_router(edge.router)
app.include_router(discovery.router)
app.include_router(risk.router)
app.include_router(settings_routes.router)
app.include_router(storage.router)
app.include_router(logs.router)
app.include_router(observability.router)
app.include_router(vault.router)
app.include_router(debug.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port, reload=False)
