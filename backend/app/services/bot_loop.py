from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from sqlmodel import Session

from app.config import get_settings
from app.models.bot import BotLoopState
from app.models.market import Market
from app.models.signal import Signal
from app.models.trade import TradeAuditRecord
from app.models.wallet import MarketFirstWalletRecord
from app.services.audit_logger import AuditLogger
from app.services.market_scanner import MarketScanner
from app.services.paper_trading_engine import PaperTradingEngine
from app.services.risk_engine import RiskDecision, RiskEngine
from app.services.signal_engine import SignalEngine
from app.services.smart_wallet_auditor import SmartWalletAuditor
from app.services.trade_audit_engine import TradeAuditEngine

logger = logging.getLogger(__name__)


BOT_MODES = ("OFF", "RESEARCH", "PAPER", "SEMI_AUTO", "LIVE")


class BotLoop:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.market_scanner = MarketScanner(session)
        self.smart_wallet_auditor = SmartWalletAuditor(session)
        self.trade_audit_engine = TradeAuditEngine(session)
        self.signal_engine = SignalEngine(session)
        self.paper_trading_engine = PaperTradingEngine(session)
        self.audit_logger = AuditLogger(session)
        self.risk_engine = RiskEngine()

    # ---------------- state helpers ----------------

    def _state(self) -> BotLoopState:
        state = self.session.get(BotLoopState, 1)
        if state is None:
            state = BotLoopState(id=1, mode=self.settings.default_bot_mode)
            self.session.add(state)
            self.session.commit()
            self.session.refresh(state)
        return state

    def status(self) -> dict[str, Any]:
        state = self._state()
        try:
            last_cycle = json.loads(state.last_cycle_summary) if state.last_cycle_summary else {}
        except Exception:
            last_cycle = {}
        return {
            "mode": state.mode,
            "running": state.running,
            "paused": state.paused,
            "kill_switch_active": state.kill_switch_active,
            "last_cycle_at": state.last_cycle_at.isoformat() if state.last_cycle_at else None,
            "last_cycle_duration_ms": state.last_cycle_duration_ms,
            "cycles_count": state.cycles_count,
            "errors_count": state.errors_count,
            "markets_scanned": state.markets_scanned,
            "wallets_audited": state.wallets_audited,
            "trades_audited": state.trades_audited,
            "signals_generated": state.signals_generated,
            "paper_trades_opened": state.paper_trades_opened,
            "rejected_signals": state.rejected_signals,
            "last_error": state.last_error,
            "last_cycle_summary": last_cycle,
        }

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in BOT_MODES:
            raise ValueError(f"Unknown mode {mode}")
        if mode == "LIVE":
            raise PermissionError("LIVE mode is intentionally disabled in v0.4")
        state = self._state()
        state.mode = mode
        if mode == "OFF":
            state.running = False
            state.paused = False
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit_logger.log("BOT_LOOP_MODE", mode, result=mode)
        logger.info("BotLoop mode set to %s", mode)
        return self.status()

    def start(self) -> dict[str, Any]:
        state = self._state()
        if state.kill_switch_active:
            return {**self.status(), "message": "kill switch active"}
        state.running = True
        state.paused = False
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit_logger.log("BOT_LOOP_START", state.mode, result="running")
        logger.info("BotLoop started in %s mode", state.mode)
        return self.status()

    def pause(self) -> dict[str, Any]:
        state = self._state()
        state.paused = True
        state.running = False
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit_logger.log("BOT_LOOP_PAUSE", state.mode, result="paused")
        return self.status()

    def stop(self) -> dict[str, Any]:
        state = self._state()
        state.running = False
        state.paused = False
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit_logger.log("BOT_LOOP_STOP", state.mode, result="stopped")
        return self.status()

    def kill_switch(self) -> dict[str, Any]:
        state = self._state()
        state.kill_switch_active = True
        state.running = False
        state.paused = False
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        self.audit_logger.log("BOT_LOOP_KILL", state.mode, result="killed")
        logger.warning("BotLoop kill switch activated")
        return self.status()

    # ---------------- one cycle ----------------

    def run_once(self, force: bool = False) -> dict[str, Any]:
        state = self._state()
        if not force and state.kill_switch_active:
            return {**self.status(), "message": "kill switch active"}
        if not force and state.mode == "OFF":
            return {**self.status(), "message": "mode OFF"}

        started = monotonic()
        markets_scanned = 0
        wallets_audited = 0
        trades_audited = 0
        signals_generated = 0
        paper_trades_opened = 0
        rejected_signals = 0
        last_error: str | None = None
        rejection_reasons: Counter[str] = Counter()

        logger.info("cycle started, mode=%s", state.mode)

        # 1. Market scan
        try:
            markets = self.market_scanner.fetch_active_markets()
            markets_scanned = len(markets)
            logger.info("markets fetched: %d", markets_scanned)
        except Exception as exc:
            last_error = f"market_scan: {exc}"
            logger.warning(last_error)

        # 2. Wallet audit (limited per cycle)
        try:
            audits = self.smart_wallet_auditor.audit_wallet_batch(limit=min(self.settings.top_wallets_target, 25))
            wallets_audited = len(audits)
            logger.info("wallets audited: %d", wallets_audited)
        except Exception as exc:
            last_error = f"wallet_audit: {exc}"
            logger.warning(last_error)

        # 3. Trade audit
        audit_records: list[TradeAuditRecord] = []
        try:
            if self.settings.trade_audit_enabled:
                audit_results = self.trade_audit_engine.audit_recent_public_trades(limit=self.settings.max_trades_per_cycle)
                trades_audited = len(audit_results)
                logger.info("trades audited: %d", trades_audited)
        except Exception as exc:
            last_error = f"trade_audit: {exc}"
            logger.warning(last_error)

        # 4. Signal generation from audits + clusters
        signals: list[Signal] = []
        try:
            audit_signals = self.signal_engine.generate_signals_from_recent_audits(limit=self.settings.max_trades_per_cycle)
            cluster_signals = self.signal_engine.generate_signals_from_recent_clusters(limit=50)
            signals = audit_signals + cluster_signals
            signals_generated = len(signals)
            logger.info("signals generated: %d", signals_generated)
        except Exception as exc:
            last_error = f"signals: {exc}"
            logger.warning(last_error)

        # Build a lookup of latest TradeAuditRecord per signal so we can feed real context
        audit_by_signal_id = self._build_audit_lookup(signals)

        # 5. Risk validation + paper trade execution
        if state.mode in ("PAPER", "SEMI_AUTO") and self.settings.auto_paper_trade_enabled:
            for signal in signals:
                audit = audit_by_signal_id.get(signal.id)
                context = self._build_audit_context(signal, audit)
                if signal.decision != "PAPER_TRADE":
                    rejection_reasons["NOT_PAPER_DECISION"] += 1
                    rejected_signals += 1
                    self._log_no_trade_for_signal(signal, audit, "NOT_PAPER_DECISION", context)
                    continue
                try:
                    result = self.paper_trading_engine.maybe_auto_trade(signal, context)
                    if result.get("executed"):
                        paper_trades_opened += 1
                        logger.info("paper trade opened: signal=%s market=%s size=$%.2f", signal.id, signal.market_id, result.get("size_usd", 0.0))
                    else:
                        rejected_signals += 1
                        reason_code = str(result.get("reason_code") or "UNKNOWN")
                        rejection_reasons[reason_code] += 1
                except Exception as exc:
                    rejected_signals += 1
                    rejection_reasons["EXCEPTION"] += 1
                    last_error = f"paper_auto: {exc}"
                    logger.warning(last_error)

        # 6. Position evaluation
        try:
            self.paper_trading_engine.evaluate_open_positions()
        except Exception as exc:
            last_error = f"position_eval: {exc}"

        elapsed_ms = round((monotonic() - started) * 1000, 2)
        state = self._state()
        state.last_cycle_at = datetime.now(UTC)
        state.last_cycle_duration_ms = elapsed_ms
        state.cycles_count += 1
        state.markets_scanned += markets_scanned
        state.wallets_audited += wallets_audited
        state.trades_audited += trades_audited
        state.signals_generated += signals_generated
        state.paper_trades_opened += paper_trades_opened
        state.rejected_signals += rejected_signals
        if last_error:
            state.errors_count += 1
            state.last_error = last_error
        cycle_summary = {
            "duration_ms": elapsed_ms,
            "markets_scanned": markets_scanned,
            "wallets_audited": wallets_audited,
            "trades_audited": trades_audited,
            "signals_generated": signals_generated,
            "paper_trades_opened": paper_trades_opened,
            "rejected_signals": rejected_signals,
            "rejection_reasons": dict(rejection_reasons),
            "last_error": last_error,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        state.last_cycle_summary = json.dumps(cycle_summary)
        state.updated_at = datetime.now(UTC)
        self.session.add(state)
        self.session.commit()
        logger.info("cycle finished: %s", cycle_summary)

        return {**cycle_summary, "status": self.status()}

    # ---------------- helpers ----------------

    def _build_audit_lookup(self, signals: list[Signal]) -> dict[str, TradeAuditRecord]:
        lookup: dict[str, TradeAuditRecord] = {}
        for signal in signals:
            if not signal.id.startswith("sig-audit-"):
                continue
            audit_id = signal.id[len("sig-audit-"):]
            audit = self.session.get(TradeAuditRecord, audit_id)
            if audit is not None:
                lookup[signal.id] = audit
        return lookup

    def _build_audit_context(self, signal: Signal, audit: TradeAuditRecord | None) -> dict[str, Any]:
        wallet = None
        try:
            if signal.wallets:
                wallets_list = json.loads(signal.wallets)
                wallet = wallets_list[0] if wallets_list else None
        except Exception:
            wallet = None
        market = self.session.get(Market, signal.market_id) if signal.market_id else None
        market_liquidity_usd = float(market.liquidity) if market and market.liquidity else 0.0
        # v0.5.2: enrich the context with the wallet's validated status so the
        # paper engine can apply the active risk-mode profile rules.
        wallet_record = self.session.get(MarketFirstWalletRecord, (wallet or "").lower()) if wallet else None
        candidate_status = wallet_record.candidate_status if wallet_record else None
        win_rate_confidence = wallet_record.win_rate_confidence if wallet_record else "INSUFFICIENT_DATA"
        market_sample_size = wallet_record.resolved_markets_traded if wallet_record else 0
        if audit is None:
            return {
                "spread_pct": 0.0,
                "liquidity": market_liquidity_usd,
                "copy_delay_seconds": 0.0,
                "price_deterioration": 0.0,
                "confidence_score": signal.confidence * 100,
                "copyable_edge_score": min(100.0, (signal.copyable_edge or 0.0) * 200),
                "wallet_tier": "INSUFFICIENT_DATA",
                "candidate_status": candidate_status,
                "win_rate_confidence": win_rate_confidence,
                "market_sample_size": market_sample_size,
                "orderbook_quality": "INSUFFICIENT_DATA",
                "liquidity_score": 0.0,
                "price": 0.5,
                "side": "BUY",
                "wallet": wallet,
                "signal_score": signal.score,
                "signal_id": signal.id,
                "market_id": signal.market_id,
            }
        return {
            "spread_pct": audit.estimated_spread or 0.0,
            "liquidity": market_liquidity_usd,
            "copy_delay_seconds": audit.copy_delay_seconds or 0.0,
            "price_deterioration": audit.price_deterioration or 0.0,
            "confidence_score": signal.confidence * 100,
            "copyable_edge_score": min(100.0, (audit.copyable_edge or 0.0) * 200),
            "wallet_tier": audit.wallet_tier,
            "candidate_status": candidate_status,
            "win_rate_confidence": win_rate_confidence,
            "market_sample_size": market_sample_size,
            "orderbook_quality": audit.orderbook_quality,
            "liquidity_score": audit.market_liquidity_score or 0.0,
            "price": audit.price or 0.5,
            "side": (audit.side or "BUY").upper(),
            "wallet": audit.wallet_address or wallet,
            "signal_score": signal.score,
            "signal_id": signal.id,
            "market_id": signal.market_id,
        }

    def _log_no_trade_for_signal(self, signal: Signal, audit: TradeAuditRecord | None, reason_code: str, context: dict[str, Any]) -> None:
        decision = RiskDecision(False, f"signal decision {signal.decision}", reason_code=reason_code)
        details = {
            "signal_decision": signal.decision,
            "signal_score": signal.score,
            "confidence": signal.confidence,
            "copyable_edge": signal.copyable_edge,
            "wallet_tier": context.get("wallet_tier"),
            "spread_pct": context.get("spread_pct"),
            "liquidity_score": context.get("liquidity_score"),
            "orderbook_quality": context.get("orderbook_quality"),
        }
        try:
            self.risk_engine.log_no_trade(
                self.session,
                decision,
                signal_id=signal.id,
                market_id=signal.market_id,
                wallet_address=context.get("wallet"),
                details=details,
            )
        except Exception as exc:
            logger.warning("no_trade_log: %s", exc)
