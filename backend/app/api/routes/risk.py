from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.database import get_session
from app.services.risk_mode import RISK_MODE_NAMES, get_profile, list_profiles
from app.services.risk_mode_service import (
    get_active_mode,
    get_active_profile,
    set_active_mode,
)

router = APIRouter(prefix="/risk", tags=["risk"])


class RiskModeUpdate(BaseModel):
    mode: str
    by: str | None = None


@router.get("/mode")
def get_mode(session: Session = Depends(get_session)) -> dict:
    profile = get_active_profile(session)
    return {
        "mode": profile.name,
        "live_allowed": profile.live_allowed,
        "available_modes": list(RISK_MODE_NAMES),
        "profile": profile.to_dict(),
    }


@router.post("/mode")
def update_mode(payload: RiskModeUpdate, session: Session = Depends(get_session)) -> dict:
    try:
        state = set_active_mode(session, payload.mode, by=payload.by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"mode": state.mode, "updated_at": state.updated_at.isoformat(), "updated_by": state.updated_by}


@router.post("/mode/{name}")
def update_mode_path(name: str, session: Session = Depends(get_session)) -> dict:
    try:
        state = set_active_mode(session, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"mode": state.mode, "updated_at": state.updated_at.isoformat()}


@router.get("/mode/limits")
def get_limits(session: Session = Depends(get_session)) -> dict:
    profile = get_active_profile(session)
    return profile.to_dict()


@router.get("/profiles")
def get_profiles() -> dict:
    return {"profiles": list_profiles(), "active_default": "SAFE"}
