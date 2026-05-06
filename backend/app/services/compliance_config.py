from app.config import Settings, get_settings


class ComplianceConfig:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def live_blocked_reason(self) -> str | None:
        if not self.settings.live_enabled:
            return "LIVE_ENABLED=false"
        if self.settings.compliance_jurisdiction == "UNSET":
            return "Compliance jurisdiction is not configured"
        if self.settings.compliance_jurisdiction not in self.settings.allowed_live_jurisdictions:
            return "Jurisdiction is not allowed for live trading"
        return None

    def is_live_allowed(self) -> bool:
        return self.live_blocked_reason() is None

    def require_user_confirmation(self, confirmed: bool) -> None:
        if not confirmed:
            raise PermissionError("Manual user confirmation is required")

    def check_jurisdiction_config(self) -> bool:
        return self.settings.compliance_jurisdiction != "UNSET"

    def block_if_live_disabled(self) -> None:
        if not self.settings.live_enabled:
            raise PermissionError("LIVE_ENABLED=false")

    def block_if_missing_credentials(self) -> None:
        raise PermissionError("Live credentials are not configured in v0.2")

    def block_if_restricted_mode(self, mode: str) -> None:
        if mode != "LIVE":
            return
        self.assert_live_allowed()

    def assert_live_allowed(self) -> None:
        reason = self.live_blocked_reason()
        if reason:
            raise PermissionError(reason)
