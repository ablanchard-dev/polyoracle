"""Tests CLOB retry queue — 2026-05-14 review Round 9 strict-compatible fix.

Pinne le contrat critique :
- maybe_queue ne queue QUE si toutes les conditions strict sont met
- idempotence (1 source_trade_id = 1 pending max)
- Worker re-fetch CLOB et n'ouvre paper QUE si gates encore strict OK
- live_enabled=True → JAMAIS queue
- Phase 1 (auto_open_paper=False) → status READY_TO_FILL, pas PaperTrade
- Phase 2 (auto_open_paper=True) → status FILLED_PAPER + PaperTrade créé
- Expiration après max_attempts ou max_age_seconds
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.market import Market
from app.models.trade import NoTradeDecision, PaperTrade, PendingClobRetry
from app.services.clob_retry_service import ClobRetryService


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _create_market(session: Session, market_id: str = "mkt-1") -> Market:
    # liquidity 50_000 → market_liquidity_score ~54 (above min_liquidity_score=30)
    # so the strict liquidity gate inside _recheck_one passes.
    m = Market(
        id=market_id,
        question="Test market",
        yes_price=0.5,
        no_price=0.5,
        liquidity=50_000.0,
        spread=0.02,
        clob_token_ids=json.dumps(["1234567890"]),
    )
    session.add(m)
    session.commit()
    return m


def _queue_args(**overrides) -> dict:
    base = dict(
        audit_id="audit-test-1",
        wallet_address="0xstrong",
        market_id="mkt-1",
        outcome="Yes",
        side="BUY",
        raw_signal_price=0.55,
        notional_usd=10.0,
        copyable_edge=0.05,
        trade_quality_score=75.0,
        wallet_tier="ELITE",
        orderbook_quality="BAD",
        market_liquidity_score=25.0,
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------- maybe_queue


def test_maybe_queue_creates_pending_when_all_strict_conditions_met() -> None:
    eng = _engine()
    with Session(eng) as session:
        _create_market(session)
        svc = ClobRetryService(session)
        # Ensure strict defaults
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        rid = svc.maybe_queue(**_queue_args())
        assert rid is not None
        pending = session.exec(select(PendingClobRetry)).all()
        assert len(pending) == 1
        assert pending[0].status == "PENDING"
        assert pending[0].source_trade_id == "audit-test-1"
        # NoTradeDecision CLOB_RETRY_QUEUED is logged
        ntds = session.exec(select(NoTradeDecision).where(NoTradeDecision.reason_code == "CLOB_RETRY_QUEUED")).all()
        assert len(ntds) == 1


def test_maybe_queue_idempotent_on_same_source_trade_id() -> None:
    eng = _engine()
    with Session(eng) as session:
        _create_market(session)
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        rid1 = svc.maybe_queue(**_queue_args(audit_id="dup"))
        rid2 = svc.maybe_queue(**_queue_args(audit_id="dup"))
        assert rid1 is not None
        assert rid2 is None  # second call returns None, no duplicate
        pending = session.exec(select(PendingClobRetry)).all()
        assert len(pending) == 1


def test_maybe_queue_blocks_when_clob_retry_disabled() -> None:
    eng = _engine()
    with Session(eng) as session:
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = False
        assert svc.maybe_queue(**_queue_args()) is None


def test_maybe_queue_blocks_when_live_enabled() -> None:
    eng = _engine()
    with Session(eng) as session:
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = True
        assert svc.maybe_queue(**_queue_args()) is None


def test_maybe_queue_rejects_non_elite_wallet() -> None:
    eng = _engine()
    with Session(eng) as session:
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        assert svc.maybe_queue(**_queue_args(wallet_tier="STRONG")) is None


def test_maybe_queue_rejects_weak_edge() -> None:
    eng = _engine()
    with Session(eng) as session:
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        # Below 0.02 STRONG_EDGE threshold
        assert svc.maybe_queue(**_queue_args(copyable_edge=0.01)) is None


def test_maybe_queue_rejects_low_trade_quality() -> None:
    eng = _engine()
    with Session(eng) as session:
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        # Below 70 threshold (sacred)
        assert svc.maybe_queue(**_queue_args(trade_quality_score=65.0)) is None


def test_maybe_queue_rejects_zero_liquidity() -> None:
    eng = _engine()
    with Session(eng) as session:
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        assert svc.maybe_queue(**_queue_args(market_liquidity_score=0.0)) is None


def test_maybe_queue_rejects_good_orderbook() -> None:
    # Si orderbook GOOD au moment audit → pas de raison de queue
    # (le trade aurait dû passer directement en PAPER_TRADE).
    eng = _engine()
    with Session(eng) as session:
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        assert svc.maybe_queue(**_queue_args(orderbook_quality="GOOD")) is None


# --------------------------------------------------------------- process_pending_batch


def _good_book_payload(token_id: str = "1234567890") -> dict:
    """OrderbookAnalyzer payload that summarizes to GOOD/ACCEPTABLE quality."""
    return {
        "token_id": token_id,
        "orderbook": {
            "bids": [{"price": 0.50, "size": 5000}, {"price": 0.49, "size": 4000}],
            "asks": [{"price": 0.51, "size": 5000}, {"price": 0.52, "size": 4000}],
        },
    }


def _bad_book_payload() -> dict:
    return {"orderbook": {"bids": [], "asks": []}}


def test_process_pending_marks_ready_when_clob_becomes_good_phase1() -> None:
    eng = _engine()
    with Session(eng) as session:
        _create_market(session)
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        svc.settings.clob_retry_auto_open_paper = False  # phase 1 collect-only

        # Backdate next_retry_at so it's due immediately
        rid = svc.maybe_queue(**_queue_args())
        pending = session.exec(select(PendingClobRetry)).one()
        pending.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(pending)
        session.commit()

        with patch.object(svc.market_scanner, "fetch_orderbook", return_value=_good_book_payload()):
            counts = svc.process_pending_batch()
        assert counts["ready"] == 1
        # Status updated, no PaperTrade created in phase 1
        refreshed = session.exec(select(PendingClobRetry)).one()
        assert refreshed.status == "READY_TO_FILL"
        papers = session.exec(select(PaperTrade)).all()
        assert len(papers) == 0


def test_process_pending_opens_paper_when_phase2_auto_open() -> None:
    eng = _engine()
    with Session(eng) as session:
        _create_market(session)
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        svc.settings.clob_retry_auto_open_paper = True

        svc.maybe_queue(**_queue_args())
        pending = session.exec(select(PendingClobRetry)).one()
        pending.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(pending)
        session.commit()

        with patch.object(svc.market_scanner, "fetch_orderbook", return_value=_good_book_payload()):
            counts = svc.process_pending_batch()
        assert counts.get("filled_paper") == 1
        refreshed = session.exec(select(PendingClobRetry)).one()
        assert refreshed.status == "FILLED_PAPER"
        papers = session.exec(select(PaperTrade)).all()
        assert len(papers) == 1


def test_process_pending_reschedules_when_still_bad() -> None:
    eng = _engine()
    with Session(eng) as session:
        _create_market(session)
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        svc.settings.clob_retry_max_attempts = 5

        svc.maybe_queue(**_queue_args())
        pending = session.exec(select(PendingClobRetry)).one()
        pending.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(pending)
        session.commit()

        with patch.object(svc.market_scanner, "fetch_orderbook", return_value=_bad_book_payload()):
            counts = svc.process_pending_batch()
        assert counts.get("retry") == 1
        refreshed = session.exec(select(PendingClobRetry)).one()
        assert refreshed.status == "PENDING"
        assert refreshed.retry_count == 1


def test_process_pending_expires_after_max_attempts() -> None:
    eng = _engine()
    with Session(eng) as session:
        _create_market(session)
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        svc.settings.clob_retry_max_attempts = 1  # expire after 1 failed attempt

        svc.maybe_queue(**_queue_args())
        pending = session.exec(select(PendingClobRetry)).one()
        pending.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(pending)
        session.commit()

        with patch.object(svc.market_scanner, "fetch_orderbook", return_value=_bad_book_payload()):
            svc.process_pending_batch()
        refreshed = session.exec(select(PendingClobRetry)).one()
        assert refreshed.status == "EXPIRED"
        ntds = session.exec(select(NoTradeDecision).where(NoTradeDecision.reason_code == "CLOB_RETRY_EXPIRED")).all()
        assert len(ntds) == 1


def test_phase2_caps_notional_at_max_trade_setting() -> None:
    # Postmortem 2026-05-14 : whale wallet trading $9000+ caused phase 2
    # workers to open papertrades with that notional, breaching exposure
    # cap (32x on $487 capital). Fix : hard cap via
    # settings.clob_retry_max_trade_notional_usd (default $20).
    eng = _engine()
    with Session(eng) as session:
        _create_market(session)
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False
        svc.settings.clob_retry_auto_open_paper = True
        svc.settings.clob_retry_max_trade_notional_usd = 20.0

        svc.maybe_queue(**_queue_args(notional_usd=9270.0))  # whale-sized
        pending = session.exec(select(PendingClobRetry)).one()
        pending.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(pending)
        session.commit()

        with patch.object(svc.market_scanner, "fetch_orderbook", return_value=_good_book_payload()):
            svc.process_pending_batch()
        papers = session.exec(select(PaperTrade)).all()
        assert len(papers) == 1
        # notional must be capped at $20, NOT $9270
        assert papers[0].notional_usd <= 20.01, f"expected ≤$20, got ${papers[0].notional_usd}"


def test_process_pending_expires_when_past_expires_at() -> None:
    eng = _engine()
    with Session(eng) as session:
        _create_market(session)
        svc = ClobRetryService(session)
        svc.settings.clob_retry_enabled = True
        svc.settings.live_enabled = False

        svc.maybe_queue(**_queue_args())
        pending = session.exec(select(PendingClobRetry)).one()
        # Force past-expiration
        pending.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=10)
        session.add(pending)
        session.commit()

        counts = svc.process_pending_batch()
        assert counts["expired"] == 1
        refreshed = session.exec(select(PendingClobRetry)).one()
        assert refreshed.status == "EXPIRED"
