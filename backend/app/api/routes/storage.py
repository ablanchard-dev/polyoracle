from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session
from app.services.storage_service import StorageService

router = APIRouter(tags=["storage"])


@router.get("/storage/status")
def get_storage_status(session: Session = Depends(get_session)) -> dict:
    return StorageService(session).status()
