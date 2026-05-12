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
        # P0.3 (2026-05-11): recompute paper_pnl on-read via PaperTradingEngine
        # to keep /bot/status coherent with /paper/report.
        # P0.2 (2026-05-11 evening, review Round 4 feedback): same recompute
        # on-read for open_positions + exposure + active_signals. BotState
        # values are stale cache, DB is truth. CRITICAL for live mode where
        # dashboard truth-source is non-negotiable.
        from sqlalchemy import func
        from sqlmodel import select as _select
        from app.models.signal import Signal
        from app.models.trade import PaperTrade
        from app.services.paper_trading_engine import PaperTradingEngine

        state = self._state()
        engine = PaperTradingEngine(self.session)
        paper_pnl = engine.compute_paper_pnl()

        # Open positions count + notional exposure from DB
        open_rows = self.session.exec(
            _select(PaperTrade).where(PaperTrade.status == "open")
        ).all()
        open_count = len(open_rows)
        exposure_usd = round(sum(float(t.notional_usd or 0.0) for t in open_rows), 4)

        # Active signals = signals with status != 'closed' or decision PAPER_TRADE not yet processed
        try:
            active_signals_count = self.session.exec(
                _select(func.count(Signal.id)).where(Signal.status == "watch")
            ).one()
            active_signals_count = int(active_signals_count or 0)
        except Exception:
            active_signals_count = state.active_signals  # fallback to stale

        return BotStatus(
            mode=state.mode,
            running=state.running,
            paused=state.paused,
            kill_switch_active=state.kill_switch_active,
            live_enabled=state.live_enabled,
            live_blocked_reason=self.compliance.live_blocked_reason(),
            paper_capital=state.paper_capital,
            paper_pnl=paper_pnl,
            exposure=exposure_usd,
            open_positions=open_count,
            active_signals=active_signals_count,
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
