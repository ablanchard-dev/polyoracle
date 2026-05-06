import json

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from app.models.bot import BotState
from app.models.signal import Signal, SignalType
from app.models.trade import NoTradeDecision
from app.services.paper_trading_engine import PaperTradingEngine


def _engine() -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_auto_trade_executes_when_all_filters_pass() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.commit()
        signal = Signal(
            id="sig-paper",
            market_id="m1",
            outcome="YES",
            signal_type=SignalType.SMART_WALLET_ENTRY,
            score=85,
            confidence=0.8,
            reason="strong wallet entry",
            decision="PAPER_TRADE",
            proposed_size_usd=10.0,
            copyable_edge=0.04,
        )
        session.add(signal)
        session.commit()
        result = PaperTradingEngine(session).maybe_auto_trade(
            signal,
            {
                "spread_pct": 0.01,
                "liquidity": 5_000,
                "copy_delay_seconds": 30,
                "price_deterioration": 0.0,
                "confidence_score": 80,
                "copyable_edge_score": 70,
                "wallet_tier": "ELITE",
                "ev_lower_bound": 0.08,
                # v0.5.2 risk-mode gate consumes candidate_status + sample + confidence
                # straight from the audit context. Provide them explicitly so we
                # don't need a MarketFirstWalletRecord seeded in this unit test.
                "candidate_status": "ELITE",
                "market_sample_size": 50,
                "win_rate_confidence": "MEDIUM",
                "orderbook_quality": "GOOD",
                "liquidity_score": 70,
                "price": 0.5,
                "side": "BUY",
                "wallet": "0xstrong",
            },
        )
    assert result["executed"] is True
    assert result["allocator"]["allocator_required"] is True


def test_auto_trade_rejects_and_logs_when_allocator_blocks() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.commit()
        signal = Signal(
            id="sig-alloc-reject",
            market_id="m1",
            outcome="YES",
            signal_type=SignalType.SMART_WALLET_ENTRY,
            score=85,
            confidence=0.8,
            reason="blocked wallet",
            decision="PAPER_TRADE",
            proposed_size_usd=10.0,
            copyable_edge=0.04,
        )
        session.add(signal)
        session.commit()
        result = PaperTradingEngine(session).maybe_auto_trade(
            signal,
            {
                "spread_pct": 0.01,
                "liquidity": 5_000,
                "copy_delay_seconds": 30,
                "price_deterioration": 0.0,
                "copyable_edge_score": 70,
                # v0.7.2 B4 fix: paper engine prefers MFWR.candidate_status,
                # then audit_context.candidate_status, then wallet_tier.
                # No MFWR seeded here → candidate_status=BLOCKED wins.
                "wallet_tier": "BLOCKED",
                "ev_lower_bound": 1.0,
                "candidate_status": "BLOCKED",
                "market_sample_size": 50,
                "win_rate_confidence": "MEDIUM",
                "orderbook_quality": "GOOD",
                "price": 0.5,
                "side": "BUY",
                "wallet": "0xblocked",
            },
        )
        no_trade = session.exec(select(NoTradeDecision)).one()
    assert result["executed"] is False
    # v0.7.2 B4: BLOCKED candidate_status now triggers the legacy
    # validate_for_mode WALLET_NOT_RELIABLE check before the allocator
    # ever runs (the legacy path is stricter — its allowed_statuses
    # excludes BLOCKED). The audit-pipeline still ends up logging the
    # same wallet rejection, just under the legacy reason_code.
    assert result["reason_code"] == "WALLET_NOT_RELIABLE"
    details = json.loads(no_trade.details or "{}")
    # The legacy log_no_trade path doesn't enrich with allocator_*.
    # We just verify the wallet was rejected for tier reasons.
    assert details.get("candidate_status") == "BLOCKED"


def test_auto_trade_blocked_when_kill_switch() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(BotState(id=1, mode="PAPER", running=False, kill_switch_active=True))
        session.commit()
        signal = Signal(
            id="sig-kill",
            market_id="m1",
            outcome="YES",
            signal_type=SignalType.SMART_WALLET_ENTRY,
            score=85,
            confidence=0.8,
            reason="kill switch",
            decision="PAPER_TRADE",
            proposed_size_usd=10.0,
            copyable_edge=0.04,
        )
        session.add(signal)
        session.commit()
        result = PaperTradingEngine(session).maybe_auto_trade(signal, {"price": 0.5, "side": "BUY"})
    assert result["executed"] is False
    assert result["reason_code"] == "KILL_SWITCH"


def test_auto_open_cannot_bypass_capital_allocator() -> None:
    engine = _engine()
    with Session(engine) as session:
        paper = PaperTradingEngine(session)
        with pytest.raises(PermissionError):
            paper.open_paper_position(
                "m1",
                "YES",
                "BUY",
                10,
                0.50,
                0.01,
                auto=True,
                notional_usd=5.0,
            )
