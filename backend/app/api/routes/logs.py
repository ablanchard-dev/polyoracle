from pathlib import Path

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/audit")
def get_audit_logs(limit: int = 100) -> list[str]:
    log_path = Path(get_settings().sqlite_path).parent / "logs" / "audit.log"
    if not log_path.exists():
        return []
    return log_path.read_text(encoding="utf-8").splitlines()[-limit:]
