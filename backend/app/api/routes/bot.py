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
