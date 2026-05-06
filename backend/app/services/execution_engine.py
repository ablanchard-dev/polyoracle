from datetime import UTC, datetime

from sqlmodel import Session

from app.config import get_settings
from app.models.bot import BotState
from app.schemas.bot import BotActionResult, BotStatus
from app.services.audit_logger import AuditLogger
from app.services.compliance_config import ComplianceConfig


class ExecutionEngine:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.compliance = ComplianceConfig(self.settings)
        self.audit = AuditLogger(session)

    def _state(self) -> BotState:
        state = self.session.get(BotState, 1)
        if state is None:
            state = BotState(
                id=1,
                mode=self.settings.default_bot_mode,
                live_enabled=self.settings.live_enabled,
                paper_capital=self.settings.paper_starting_capital,
            )
            self.session.add(state)
            self.session.commit()
            self.session.refresh(state)
        return state

    def status(self) -> BotStatus:
        state = self._state()
        return BotStatus(
            mode=state.mode,
            running=state.running,
            paused=state.paused,
            kill_switch_active=state.kill_switch_active,
            live_enabled=state.live_enabled,
            live_blocked_reason=self.compliance.live_blocked_reason(),
            paper_capital=state.paper_capital,
            paper_pnl=state.paper_pnl,
            exposure=state.exposure,
            open_positions=state.open_positions,
            active_signals=state.active_signals,
            last_error=state.last_error,
        )

    def start(self) -> BotActionResult:
        state = self._state()
        if state.kill_switch_active:
            return BotActionResult(status=self.status(), message="Kill switch active. Stop and review before restart.")
        if state.mode == "LIVE":
            self.compliance.assert_live_allowed()
        state.running = True
        state.paused = False
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit.log("BOT_START", state.mode, result="started")
        return BotActionResult(status=self.status(), message="Bot started")

    def pause(self) -> BotActionResult:
        state = self._state()
        state.paused = True
        state.running = False
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit.log("BOT_PAUSE", state.mode, result="paused")
        return BotActionResult(status=self.status(), message="Bot paused")

    def stop(self) -> BotActionResult:
        state = self._state()
        state.running = False
        state.paused = False
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit.log("BOT_STOP", state.mode, result="stopped")
        return BotActionResult(status=self.status(), message="Bot stopped")

    def emergency_stop(self) -> BotActionResult:
        state = self._state()
        state.running = False
        state.paused = False
        state.kill_switch_active = True
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit.log("KILL_SWITCH", state.mode, result="activated")
        return BotActionResult(status=self.status(), message="Emergency stop activated")

    def prepare_order(self, payload: dict) -> dict:
        return {"prepared": True, "payload": payload}

    def validate_order_with_risk(self, payload: dict) -> bool:
        return bool(payload)

    def submit_order_live(self, payload: dict, user_confirmed: bool = False) -> dict:
        if not user_confirmed:
            raise PermissionError("Manual user confirmation is required for live orders")
        self.compliance.assert_live_allowed()
        raise PermissionError("Live trading implementation is intentionally disabled in v0.1")

    def cancel_order(self, order_id: str) -> dict[str, str]:
        self.compliance.assert_live_allowed()
        return {"order_id": order_id, "status": "not_sent_v0_1"}

    def cancel_all_orders(self) -> dict[str, str]:
        self.compliance.assert_live_allowed()
        return {"status": "not_sent_v0_1"}

    def close_position(self, position_id: str) -> dict[str, str]:
        return {"position_id": position_id, "status": "paper_only_v0_1"}

    def switch_mode(self, mode: str) -> BotActionResult:
        state = self._state()
        if mode == "LIVE":
            self.compliance.assert_live_allowed()
        state.mode = mode
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        return BotActionResult(status=self.status(), message=f"Mode switched to {mode}")
