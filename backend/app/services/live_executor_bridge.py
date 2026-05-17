"""Live executor bridge — branche le bot vers Polymarket CLOB en mode live.

DOCTRINE : MÊME comportement paper vs live. Le bot a DÉJÀ toutes les règles
(max_pos par tier, exposure cap, R-sizing, audit gates, cluster dedup,
duration filter). Cette bridge ne fait QUE :
- Si LIVE_ENABLED=false → return paper mode (rien à faire, bot continue normal)
- Si LIVE_ENABLED=true → place CLOB order via clob_executor, return order_id

PAS de hard caps additionnels. Les règles du bot sont les règles du live.

Token_id resolver : pour passer de (market_id, outcome) à un token_id Polymarket,
on lit le market dans DB qui contient clob_token_ids JSON.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_executor_singleton: Any = None
_singleton_lock = threading.Lock()


def get_executor():
    """Singleton CLOBExecutor (instancié 1× au boot, réutilisé)."""
    global _executor_singleton
    if _executor_singleton is not None:
        return _executor_singleton
    with _singleton_lock:
        if _executor_singleton is not None:
            return _executor_singleton
        try:
            from app.services.clob_executor import build_executor_from_env
            exe = build_executor_from_env()
            if exe is None:
                logger.warning("live_executor_bridge: build_executor_from_env returned None")
                return None
            exe.initialize()
            _executor_singleton = exe
            logger.info(
                "live_executor_bridge: executor initialized sig_type=%s funder=%s",
                exe.config.signature_type, exe.config.funder_address,
            )
            return exe
        except Exception as e:
            logger.error(f"live_executor_bridge: executor init failed: {type(e).__name__}: {e}")
            return None


def _resolve_token_id(session, market_id: str, outcome: str) -> str | None:
    """Resolve Polymarket token_id from market_id + outcome name.

    Market record stores clob_token_ids as JSON list matching outcomes order.
    """
    try:
        from app.models.market import Market
    except Exception:
        return None
    market = session.get(Market, market_id)
    if not market:
        return None
    raw_tokens = getattr(market, "clob_token_ids", None)
    raw_outcomes = getattr(market, "outcomes", None)
    if not raw_tokens or not raw_outcomes:
        return None
    try:
        tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
    except Exception:
        return None
    if not isinstance(tokens, list) or not isinstance(outcomes, list):
        return None
    for i, o in enumerate(outcomes):
        if str(o).strip().lower() == outcome.strip().lower():
            if i < len(tokens):
                return str(tokens[i])
    return None


def decide_and_place_live(
    *,
    session,
    market_id: str,
    outcome: str,
    side: str,
    price: float,
    notional_usd: float,
    signal_id: str | None = None,
    wallet_address: str | None = None,
) -> dict[str, Any]:
    """Main entry. Returns dict with mode/success/order_id/fill_price/error.

    mode:
      - "paper" : LIVE_ENABLED=false, caller proceeds with normal paper open
      - "live"  : LIVE_ENABLED=true, CLOB order placed successfully
      - "skip"  : LIVE_ENABLED=true mais erreur fatale (token unresolvable,
                  exec down). Caller doit SKIP ce signal (pas de fallback paper
                  pour éviter divergence accounting paper/live).
    """
    try:
        from app.config import get_settings
        settings = get_settings()
    except Exception:
        return {"mode": "paper", "success": True, "skip_reason": None}

    if not settings.live_enabled:
        return {"mode": "paper", "success": True, "skip_reason": "LIVE_DISABLED"}

    # Resolve token_id (mandatory)
    token_id = _resolve_token_id(session, market_id, outcome)
    if not token_id:
        return {
            "mode": "skip", "success": False,
            "skip_reason": f"TOKEN_ID_NOT_RESOLVABLE market={market_id[:10]}... outcome={outcome}",
        }

    # Get executor
    exe = get_executor()
    if exe is None:
        return {
            "mode": "skip", "success": False,
            "skip_reason": "EXECUTOR_NOT_AVAILABLE",
        }

    # Place CLOB order (FOK = fill-or-kill, no partial leftover)
    try:
        result = exe.place_order(
            token_id=token_id,
            side=side,
            price=price,
            notional_usd=notional_usd,
            order_type="FOK",
        )
    except Exception as e:
        logger.error(f"place_order exception: {type(e).__name__}: {e}")
        return {
            "mode": "skip", "success": False,
            "skip_reason": f"CLOB_PLACE_EXCEPTION_{type(e).__name__}",
            "error": str(e),
        }

    if not result.success:
        return {
            "mode": "skip", "success": False,
            "skip_reason": "CLOB_REJECTED",
            "error": result.error,
        }

    logger.info(
        "LIVE ORDER PLACED order_id=%s token=%s side=%s price=%s notional=$%s",
        result.order_id, token_id[:12], side, price, notional_usd,
    )
    return {
        "mode": "live",
        "success": True,
        "order_id": result.order_id,
        "fill_price": result.price,
        "skip_reason": None,
        "error": None,
    }


def status() -> dict[str, Any]:
    """Bridge status for observability endpoint."""
    try:
        from app.config import get_settings
        settings = get_settings()
    except Exception:
        return {"live_enabled": False, "ready": False}
    exe = _executor_singleton  # don't init here, just report state
    return {
        "live_enabled": settings.live_enabled,
        "executor_initialized": exe is not None,
    }
