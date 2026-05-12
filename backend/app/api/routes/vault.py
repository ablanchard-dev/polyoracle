"""Vault status route (Phase E E-VAULT skeleton — 2026-05-11).

Read-only endpoint exposing the HWM vault state simulation. No live tx yet.
Activation post-Phase E (live execution + EOA configured)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session

router = APIRouter(prefix="/vault", tags=["vault"])


@router.get("/status")
def vault_status(session: Session = Depends(get_session)) -> dict[str, Any]:
    """HWM vault status. Mode SIMULATION en Phase A — montre la projection
    transfer/sizing_mode basée sur paper_capital + realized_pnl post-cutover.
    Quand Phase E activé : remplace par real Polygon USDC.e transfers."""
    from app.services.vault_manager import vault_status_payload
    return vault_status_payload(session)
