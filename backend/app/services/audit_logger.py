from uuid import uuid4
from pathlib import Path

from sqlmodel import Session

from app.models.audit import AuditEvent
from app.config import get_settings


class AuditLogger:
    def __init__(self, session: Session) -> None:
        self.session = session

    def log(
        self,
        event_type: str,
        bot_mode: str,
        market_id: str | None = None,
        outcome: str | None = None,
        price: float | None = None,
        signal_id: str | None = None,
        score: float | None = None,
        reason: str | None = None,
        risk_decision: str | None = None,
        order_payload: str | None = None,
        result: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=str(uuid4()),
            event_type=event_type,
            bot_mode=bot_mode,
            market_id=market_id,
            outcome=outcome,
            price=price,
            signal_id=signal_id,
            score=score,
            reason=reason,
            risk_decision=risk_decision,
            order_payload=order_payload,
            result=result,
        )
        self.session.add(event)
        self.session.commit()
        log_path = Path(get_settings().resolved_sqlite_path).parent / "logs" / "audit.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8") if not log_path.exists() else None
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{event.created_at.isoformat()} {event.event_type} mode={event.bot_mode} result={event.result or ''}\n")
        return event
