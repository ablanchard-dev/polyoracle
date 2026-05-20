"""Live executor bridge v2 — POLYORACLE → Polymarket CLOB.

DOCTRINE LIVE_READY_STRICT (2026-05-20) :
- Submit FAK market order BEFORE paper open
- Paper mirror = exact live fill (avg_fill_price, filled_shares, filled_notional)
- Si CLOB non fillable → SKIP (pas de paper open, pas de fallback Gamma)
- Si live fail / partial below threshold → SKIP
- Slippage cap calculé DYNAMIQUEMENT via compute_max_acceptable_price (pas 0.99)
- Utilise cache WS via polymarket_ws_orderbook si disponible

FLAGS :
- LIVE_EXECUTOR_V2_ENABLED : code v2 chargé
- LIVE_READY_STRICT       : execution parity active
- LIVE_ENABLED            : submit ordres réels
- E8_ONLY                 : seul E8 script peut submit

Caller (paper_trading_engine.maybe_auto_trade) doit utiliser :
- result["mode"] == "live" → ouvrir paper avec result["avg_fill_price"] + result["filled_shares"]
- result["mode"] == "skip" → return executed=False, NE PAS ouvrir paper
- result["mode"] == "paper" → LIVE_ENABLED=false ET v2_enabled=false : comportement paper legacy
- result["mode"] == "paper_strict_ready" → v2_enabled=true, LIVE_ENABLED=false, on a PRE-CHECKED
  fillable et on retourne le VWAP comme execution_price pour paper mirror
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_executor_singleton: Any = None
_singleton_lock = threading.Lock()


def get_executor():
    """Singleton CLOBExecutor v2 (built once at boot)."""
    global _executor_singleton
    if _executor_singleton is not None:
        return _executor_singleton
    with _singleton_lock:
        if _executor_singleton is not None:
            return _executor_singleton
        try:
            from app.services.clob_executor import build_executor_from_env, is_v2_enabled
            if not is_v2_enabled():
                return None
            exe = build_executor_from_env()
            if exe is None:
                logger.warning("live_executor_bridge: build_executor_from_env returned None")
                return None
            exe.initialize()
            _executor_singleton = exe
            logger.info("live_executor_bridge v2 OK")
            return exe
        except Exception as e:
            logger.error("bridge init failed: %s: %s", type(e).__name__, e)
            return None


def _resolve_token_id(session, market_id: str, outcome: str) -> Optional[str]:
    """Resolve Polymarket token_id from market_id + outcome name. UNCHANGED from v1."""
    try:
        from app.models.market import Market
    except Exception:
        return None
    market = session.get(Market, market_id)
    if not market:
        return None
    raw_tokens = getattr(market, "clob_token_ids", None)
    raw_outcomes = getattr(market, "outcomes", None) or '["Yes", "No"]'
    if not raw_tokens:
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
    # Heuristic fallback
    up_words = {"yes", "up", "buy", "true", "1"}
    if outcome.strip().lower() in up_words and len(tokens) >= 1:
        return str(tokens[0])
    if len(tokens) >= 2:
        return str(tokens[1])
    return None


def resolve_token_id_public(session, market_id: str, outcome: str) -> Optional[str]:
    """Public wrapper for callers like paper_trading_engine."""
    return _resolve_token_id(session, market_id, outcome)


def decide_and_place_live(
    *,
    session,
    market_id: str,
    outcome: str,
    side: str,
    notional_usd: float,
    gamma_mid_reference: Optional[float] = None,
    signal_id: Optional[str] = None,
    wallet_address: Optional[str] = None,
    e8_authorized: bool = False,
) -> dict[str, Any]:
    """Main entry. Returns dict with mode + fill details.

    Modes :
      - "paper"             : v2 not enabled, caller does legacy paper
      - "paper_strict_ready": v2 enabled, LIVE_ENABLED=false. PRE-CHECK fillable +
                              return vwap_price for paper mirror. No live order.
      - "live"              : LIVE_ENABLED=true (or e8_authorized). FAK submitted, fill confirmed.
      - "skip"              : CLOB not fillable / no fill / partial below threshold.

    gamma_mid_reference : optional gamma mid for slippage cap dynamic calc.
    e8_authorized       : True only from _e8_single_live_trade.py (bypasses E8_ONLY).
    """
    try:
        from app.services.clob_executor import (
            is_v2_enabled, is_live_enabled, is_strict_enabled, is_e8_only,
        )
    except Exception:
        return {"mode": "paper", "success": True, "skip_reason": None}

    # If v2 not enabled at all → legacy paper
    if not is_v2_enabled():
        return {"mode": "paper", "success": True, "skip_reason": "V2_DISABLED"}

    # Resolve token_id
    token_id = _resolve_token_id(session, market_id, outcome)
    if not token_id:
        return {
            "mode": "skip", "success": False,
            "skip_reason": f"TOKEN_ID_NOT_RESOLVABLE market={market_id[:10]}... outcome={outcome}",
        }

    exe = get_executor()
    if exe is None:
        return {"mode": "skip", "success": False, "skip_reason": "EXECUTOR_NOT_AVAILABLE"}

    # If LIVE_ENABLED=false (and not E8), we operate in paper_strict_ready :
    # pre-check fillable + return VWAP as execution price for paper mirror
    if not is_live_enabled() and not e8_authorized:
        vwap, status, audit = exe.get_executable_price(
            token_id, side.upper(), float(notional_usd), gamma_mid=gamma_mid_reference,
        )
        if status != "OK_FILLABLE" or vwap is None:
            return {
                "mode": "skip", "success": False,
                "skip_reason": f"CLOB_NOT_LIVE_FILLABLE_{status}",
                "audit": audit,
            }
        return {
            "mode": "paper_strict_ready",
            "success": True,
            "vwap_price": vwap,
            "audit": audit,
            "skip_reason": None,
        }

    # LIVE_ENABLED=true (or e8_authorized) : actual submit FAK
    try:
        result = exe.place_order(
            token_id=token_id,
            side=side.upper(),
            notional_usd=float(notional_usd),
            gamma_mid_reference=gamma_mid_reference,
            e8_authorized=e8_authorized,
        )
    except Exception as e:
        logger.error("place_order exc: %s: %s", type(e).__name__, e)
        return {
            "mode": "skip", "success": False,
            "skip_reason": f"CLOB_PLACE_EXCEPTION_{type(e).__name__}",
            "error": str(e),
        }

    if not result.success:
        skip_map = {
            "NO_ORDERBOOK": "CLOB_NOT_LIVE_FILLABLE",
            "INSUFFICIENT_DEPTH": "CLOB_NOT_LIVE_FILLABLE",
            "NO_FILL": "LIVE_NO_FILL",
            "PARTIAL_FILL_REJECTED": "LIVE_PARTIAL_BELOW_THRESHOLD",
            "SLIPPAGE_TOO_HIGH": "LIVE_SLIPPAGE_GUARD",
            "SKIP_E8_ONLY": "BLOCKED_E8_ONLY",
            "SKIP_NOT_LIVE_ENABLED": "BLOCKED_LIVE_DISABLED",
            "REJECTED": "LIVE_REJECTED",
            "EXCEPTION": "LIVE_EXCEPTION",
        }
        skip_reason = skip_map.get(result.fill_status, f"LIVE_FAIL_{result.fill_status}")
        return {
            "mode": "skip", "success": False,
            "skip_reason": skip_reason,
            "fill_status": result.fill_status,
            "error": result.error,
            "vwap_computed": result.vwap_computed,
            "slippage_cap_used": result.slippage_cap_used,
        }

    logger.info(
        "LIVE FILL token=%s side=%s req=$%s filled=$%.4f shares=%.4f avg=%.4f "
        "vwap=%.4f cap=%.4f status=%s latency=%dms order=%s src=%s",
        token_id[:12], side, notional_usd, result.filled_notional, result.filled_shares,
        result.avg_fill_price, result.vwap_computed, result.slippage_cap_used,
        result.fill_status, result.latency_ms, result.order_id, result.price_source,
    )
    return {
        "mode": "live",
        "success": True,
        "order_id": result.order_id,
        "trade_ids": result.trade_ids,
        "filled_shares": result.filled_shares,
        "filled_notional": result.filled_notional,
        "avg_fill_price": result.avg_fill_price,
        "fees_paid": result.fees_paid,
        "fill_status": result.fill_status,
        "latency_ms": result.latency_ms,
        "slippage_cap_used": result.slippage_cap_used,
        "vwap_computed": result.vwap_computed,
        "price_source": result.price_source,
        "skip_reason": None,
        "error": None,
    }


def status() -> dict[str, Any]:
    try:
        from app.services.clob_executor import (
            is_v2_enabled, is_live_enabled, is_strict_enabled, is_e8_only,
        )
    except Exception:
        return {"v2_enabled": False, "ready": False}
    exe = _executor_singleton
    return {
        "v2_enabled": is_v2_enabled(),
        "strict_enabled": is_strict_enabled(),
        "live_enabled": is_live_enabled(),
        "e8_only": is_e8_only(),
        "executor_initialized": exe is not None,
        "sdk_version": "py-clob-client-v2",
    }
