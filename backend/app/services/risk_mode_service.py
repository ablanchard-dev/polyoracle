"""Read/write helper for the active ``RiskModeState`` row.

Kept tiny on purpose: the policy lives in ``risk_mode.py``, this just owns the
single-row SQLModel that the UI and the bot loop read from.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session

from app.config import get_settings
from app.models.bot import RiskModeState
from app.services.risk_mode import (
    RISK_MODE_NAMES,
    RiskModeName,
    RiskModeProfile,
    get_profile,
)


def _ensure_state(session: Session) -> RiskModeState:
    state = session.get(RiskModeState, 1)
    if state is None:
        default = (get_settings().risk_mode or "SAFE").upper()
        state = RiskModeState(id=1, mode=default if default in RISK_MODE_NAMES else "SAFE")
        session.add(state)
        session.commit()
        session.refresh(state)
    return state


def get_active_mode(session: Session) -> RiskModeName:
    state = _ensure_state(session)
    mode = state.mode if state.mode in RISK_MODE_NAMES else "SAFE"
    return mode  # type: ignore[return-value]


def get_active_profile(session: Session) -> RiskModeProfile:
    return get_profile(get_active_mode(session))


def set_active_mode(session: Session, mode: str, *, by: str | None = None) -> RiskModeState:
    upper = mode.upper().strip()
    if upper not in RISK_MODE_NAMES:
        raise ValueError(f"Unknown risk mode {mode!r}. Allowed: {', '.join(RISK_MODE_NAMES)}")
    state = _ensure_state(session)
    state.mode = upper
    state.updated_at = datetime.now(UTC)
    state.updated_by = by
    session.add(state)
    session.commit()
    session.refresh(state)
    return state
