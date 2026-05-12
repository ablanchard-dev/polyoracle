from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.database import get_session
from app.schemas.bot import BotActionResult, BotStatus
from app.services.bot_loop import BotLoop
from app.services.execution_engine import ExecutionEngine
from app.services.wallet_polling_engine import WalletPollingEngine

router = APIRouter(prefix="/bot", tags=["bot"])


@router.get("/status", response_model=BotStatus)
def get_bot_status(session: Session = Depends(get_session)) -> BotStatus:
    return ExecutionEngine(session).status()


@router.post("/start", response_model=BotActionResult)
def start_bot(session: Session = Depends(get_session)) -> BotActionResult:
    return ExecutionEngine(session).start()


@router.post("/pause", response_model=BotActionResult)
def pause_bot(session: Session = Depends(get_session)) -> BotActionResult:
    return ExecutionEngine(session).pause()


@router.post("/stop", response_model=BotActionResult)
def stop_bot(session: Session = Depends(get_session)) -> BotActionResult:
    return ExecutionEngine(session).stop()


@router.post("/kill-switch", response_model=BotActionResult)
def kill_switch(session: Session = Depends(get_session)) -> BotActionResult:
    BotLoop(session).kill_switch()
    return ExecutionEngine(session).emergency_stop()


# ---------------- bot loop ----------------


@router.get("/loop/status")
def loop_status(session: Session = Depends(get_session)) -> dict:
    return BotLoop(session).status()


@router.post("/mode/research")
def mode_research(session: Session = Depends(get_session)) -> dict:
    BotLoop(session).set_mode("RESEARCH")
    return ExecutionEngine(session).switch_mode("RESEARCH").model_dump()


@router.post("/mode/paper")
def mode_paper(session: Session = Depends(get_session)) -> dict:
    BotLoop(session).set_mode("PAPER")
    return ExecutionEngine(session).switch_mode("PAPER").model_dump()


@router.post("/mode/off")
def mode_off(session: Session = Depends(get_session)) -> dict:
    BotLoop(session).set_mode("OFF")
    return ExecutionEngine(session).switch_mode("OFF").model_dump()


@router.post("/audit/start")
def audit_start(session: Session = Depends(get_session)) -> dict:
    return BotLoop(session).start()


@router.post("/audit/stop")
def audit_stop(session: Session = Depends(get_session)) -> dict:
    return BotLoop(session).stop()


@router.post("/audit/run-once")
def audit_run_once(session: Session = Depends(get_session)) -> dict:
    try:
        return BotLoop(session).run_once(force=True)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# ---------------- wallet polling (v0.5.8) ----------------


@router.post("/polling/start")
async def polling_start() -> dict:
    try:
        return await WalletPollingEngine.instance().start()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/polling/stop")
async def polling_stop() -> dict:
    return await WalletPollingEngine.instance().stop()


@router.get("/polling/status")
def polling_status() -> dict:
    return WalletPollingEngine.instance().status()


# ---------------- A-T0 strict cutover (Phase A — 2026-05-11) ----------------


@router.post("/strict-cutover")
def strict_cutover(session: Session = Depends(get_session)) -> dict:
    """Mark the PAPER_LIVE_STRICT cutover. Trades opened BEFORE this
    timestamp were taken under the legacy ELITE-paper bypass and are
    NOT decisional for live go/no-go. To be called once when starting
    Phase B baseline strict (after PAPER_LIVE_STRICT=true is enabled).
    Idempotent: re-calling overwrites the marker (bot loop preserves
    BotState.paper_capital, no capital reset)."""
    from datetime import UTC as _UTC, datetime as _datetime
    from app.models.bot import BotState
    from app.services.audit_logger import AuditLogger

    state = session.get(BotState, 1)
    if state is None:
        raise HTTPException(status_code=400, detail="BotState not initialised")
    now = _datetime.now(_UTC)
    state.strict_cutover_at = now
    state.updated_at = now
    session.add(state)
    AuditLogger(session).log(
        "STRICT_CUTOVER",
        state.mode,
        result=f"cutover_at={now.isoformat()}",
    )
    session.commit()
    return {
        "strict_cutover_at": now.isoformat(),
        "paper_capital_preserved": state.paper_capital,
        "message": "Cutover recorded. Trades opened after this are decisional.",
    }


@router.get("/strict-cutover")
def strict_cutover_status(session: Session = Depends(get_session)) -> dict:
    from app.models.bot import BotState
    state = session.get(BotState, 1)
    if state is None or state.strict_cutover_at is None:
        return {"strict_cutover_at": None, "trades_after_cutover": 0}
    cutover = state.strict_cutover_at
    if cutover.tzinfo is None:
        from datetime import UTC as _UTC
        cutover = cutover.replace(tzinfo=_UTC)
    from sqlmodel import select as _select
    from app.models.trade import PaperTrade
    after = list(session.exec(
        _select(PaperTrade).where(PaperTrade.opened_at >= cutover)
    ).all())
    return {
        "strict_cutover_at": cutover.isoformat(),
        "trades_after_cutover": len(after),
        "paper_capital": state.paper_capital,
    }
