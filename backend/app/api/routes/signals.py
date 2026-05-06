from collections import Counter

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session
from app.schemas.signal import SignalRead
from app.services.signal_engine import SignalEngine

router = APIRouter(tags=["signals"])


def _is_active(signal) -> bool:
    decision = (getattr(signal, "decision", None) or "").upper()
    status = (signal.status or "").lower()
    if decision == "REJECT":
        return False
    if status in {"rejected", "reject"}:
        return False
    return True


@router.get("/signals", response_model=list[SignalRead])
def get_signals(session: Session = Depends(get_session)) -> list[SignalRead]:
    return SignalEngine(session).generate_signals()


@router.get("/signals/active", response_model=list[SignalRead])
def get_active_signals(session: Session = Depends(get_session)) -> list[SignalRead]:
    return [signal for signal in SignalEngine(session).generate_signals() if _is_active(signal)]


@router.get("/signals/rejected", response_model=list[SignalRead])
def get_rejected_signals(session: Session = Depends(get_session)) -> list[SignalRead]:
    signals = SignalEngine(session).generate_signals()
    return [signal for signal in signals if not _is_active(signal)]


@router.get("/signals/decisions")
def get_signal_decision_breakdown(session: Session = Depends(get_session)) -> dict[str, int]:
    signals = SignalEngine(session).generate_signals()
    counter: Counter[str] = Counter()
    for signal in signals:
        decision = (getattr(signal, "decision", None) or signal.status or "UNKNOWN").upper()
        counter[decision] += 1
    return dict(counter)
