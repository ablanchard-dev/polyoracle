from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class Notification:
    level: str
    title: str
    body: str
    created_at: datetime


class NotificationEngine:
    def send_ui_alert(self, level: str, title: str, body: str) -> Notification:
        return Notification(level=level, title=title, body=body, created_at=datetime.now(UTC))
