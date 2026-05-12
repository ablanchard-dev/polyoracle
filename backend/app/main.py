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
    # 2026-05-09 — auto-reclass cron retiré: tournerait à vide tant que B22
    # (audit_at MFWR figé depuis 28-29 avril, voir spec.md) n'est pas résolu.
    # À ré-activer une fois le mécanisme W+L incremental update implémenté
    # (réutilisation des trades polling sans nouveaux API calls).


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
