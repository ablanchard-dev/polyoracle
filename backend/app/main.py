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
