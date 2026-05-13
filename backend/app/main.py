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

    # Phase G (Round 8, 2026-05-13) — auto-reclass quotidien REACTIVÉ.
    # B22 incremental is now wired into _close_paper_with_reason (refresh MFWR
    # on each RESOLVED close). The daily reclass thus has fresh W+L data to
    # work with — promotions/demotions emerge naturally as the cohort evolves.
    # Previous concern (2026-05-09 "cron tournerait à vide") is resolved.
    try:
        import asyncio
        import logging
        from datetime import datetime, timezone
        from app.services.weekly_reclass_service import run_weekly_reclass
        from app.database import engine
        from sqlmodel import Session

        async def _daily_reclass_loop():
            await asyncio.sleep(60)  # boot grace period
            while True:
                try:
                    with Session(engine) as session:
                        result = run_weekly_reclass(session, dry_run=False)
                    promoted = len(result.get("promoted", []))
                    demoted = len(result.get("demoted", []))
                    if promoted or demoted:
                        logging.getLogger(__name__).info(
                            "auto-reclass daily: +%d ELITE, -%d demoted",
                            promoted, demoted,
                        )
                        # Force cohort reload at next polling cycle
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
                # Sleep 24h before next run
                await asyncio.sleep(86400)

        asyncio.create_task(_daily_reclass_loop())
    except Exception:
        pass  # never crash startup on auto-reclass


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
