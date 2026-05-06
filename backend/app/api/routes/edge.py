from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session
from app.services.edge_validation_engine import EdgeValidationEngine

router = APIRouter(prefix="/edge", tags=["edge"])


@router.get("/report")
def get_edge_report(session: Session = Depends(get_session)) -> dict:
    return EdgeValidationEngine(session).generate_edge_report()


@router.get("/metrics")
def get_edge_metrics(session: Session = Depends(get_session)) -> dict:
    return EdgeValidationEngine(session).metrics().__dict__


@router.get("/strategies")
def get_strategies(session: Session = Depends(get_session)) -> dict:
    return EdgeValidationEngine(session).compare_strategies()


@router.get("/wallets")
def get_wallet_breakdown(session: Session = Depends(get_session)) -> list[dict]:
    return EdgeValidationEngine(session).wallet_breakdown()


@router.get("/categories")
def get_category_breakdown(session: Session = Depends(get_session)) -> list[dict]:
    return EdgeValidationEngine(session).category_breakdown()


@router.get("/no-trade-log")
def get_no_trade_log(limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    return EdgeValidationEngine(session).no_trade_decision_log(limit=limit)
