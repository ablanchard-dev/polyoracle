from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from app.database import get_session
from app.services.trade_audit_engine import TradeAuditEngine

router = APIRouter(prefix="/trades", tags=["trades"])


class TradeAuditRunRequest(BaseModel):
    limit: int | None = None


def _serialize_audit(record) -> dict:
    return {
        "id": record.id,
        "trade_id": record.trade_id,
        "wallet_address": record.wallet_address,
        "market_id": record.market_id,
        "question": record.question,
        "outcome": record.outcome,
        "side": record.side,
        "price": record.price,
        "size": record.size,
        "notional_usd": record.notional_usd,
        "estimated_spread": record.estimated_spread,
        "estimated_slippage": record.estimated_slippage,
        "wallet_score": record.wallet_score,
        "wallet_tier": record.wallet_tier,
        "market_liquidity_score": record.market_liquidity_score,
        "orderbook_quality": record.orderbook_quality,
        "copy_delay_seconds": record.copy_delay_seconds,
        "price_deterioration": record.price_deterioration,
        "copyable_edge": record.copyable_edge,
        "trade_quality_score": record.trade_quality_score,
        "decision": record.decision,
        "reasons": record.reasons,
        "warnings": record.warnings,
        "audited_at": record.audited_at.isoformat() if record.audited_at else None,
    }


@router.get("/recent")
def recent_trades(limit: int = 200, session: Session = Depends(get_session)) -> list[dict]:
    engine = TradeAuditEngine(session)
    return [
        {
            "id": trade.id,
            "wallet_address": trade.wallet_address,
            "market_id": trade.market_id,
            "outcome": trade.outcome,
            "side": trade.side,
            "price": trade.price,
            "size": trade.size,
            "notional_usd": trade.notional_usd,
            "traded_at": trade.traded_at.isoformat() if trade.traded_at else None,
            "data_source": trade.data_source,
            "seen_at": trade.seen_at.isoformat() if trade.seen_at else None,
        }
        for trade in engine.list_recent_public_trades(limit=limit)
    ]


@router.get("/audited")
def audited_trades(limit: int = 200, session: Session = Depends(get_session)) -> list[dict]:
    return [_serialize_audit(record) for record in TradeAuditEngine(session).list_audited_trades(limit=limit)]


@router.get("/clusters")
def trade_clusters(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    engine = TradeAuditEngine(session)
    return [
        {
            "id": cluster.id,
            "market_id": cluster.market_id,
            "outcome": cluster.outcome,
            "side": cluster.side,
            "wallet_count": cluster.wallet_count,
            "trade_count": cluster.trade_count,
            "notional_usd": cluster.notional_usd,
            "average_price": cluster.average_price,
            "average_wallet_score": cluster.average_wallet_score,
            "started_at": cluster.started_at.isoformat() if cluster.started_at else None,
            "ended_at": cluster.ended_at.isoformat() if cluster.ended_at else None,
            "confidence": cluster.confidence,
            "detected_at": cluster.detected_at.isoformat() if cluster.detected_at else None,
        }
        for cluster in engine.list_clusters(limit=limit)
    ]


@router.get("/large")
def large_trades(limit: int = 100, threshold_usd: float = 25_000, session: Session = Depends(get_session)) -> list[dict]:
    return [_serialize_audit(record) for record in TradeAuditEngine(session).list_large_trades(limit=limit, threshold_usd=threshold_usd)]


@router.get("/smart-money")
def smart_money_events(limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "wallet_address": event.wallet_address,
            "market_id": event.market_id,
            "outcome": event.outcome,
            "notional_usd": event.notional_usd,
            "price": event.price,
            "confidence": event.confidence,
            "summary": event.summary,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
        for event in TradeAuditEngine(session).list_smart_money_events(limit=limit)
    ]


@router.post("/audit/run")
def run_trade_audit(payload: TradeAuditRunRequest, session: Session = Depends(get_session)) -> dict:
    results = TradeAuditEngine(session).audit_recent_public_trades(limit=payload.limit)
    return {"audited": len(results), "results": [result.to_dict() for result in results]}


@router.get("/stats")
def trade_audit_stats(session: Session = Depends(get_session)) -> dict:
    return TradeAuditEngine(session).stats()
