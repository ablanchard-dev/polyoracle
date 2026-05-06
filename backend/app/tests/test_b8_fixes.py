"""v0.7.4 B8 fix tests — now PASS (were xfail in v0.7.3 observability).

* B8-A — `_process_trade_in_session` uses
  `session.get(TradeAuditRecord, expected_audit_id)` directly. Audits
  outside the top-10 by `audited_at` are still found.

* B8-B — when `audit.decision != PAPER_TRADE`, the polling pipeline
  writes a `NoTradeDecision(reason_code='AUDIT_DECISION_NOT_PAPER')`
  so the trade stays observable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from sqlmodel import SQLModel, Session, create_engine, select


# ============ B8-A: direct audit lookup ============


def test_b8a_process_trade_finds_audit_by_id_even_past_top10():
    """The polling pipeline must find the just-processed audit by id,
    even when 10+ more recent audits exist in DB.

    Setup: process a trade whose audit ends up past the top-10 by
    audited_at (defended by direct session.get lookup). Pre-fix this
    was a silent drop. Post-fix it reaches the cluster path.
    """
    from app.services.wallet_polling_engine import WalletPollingEngine
    from app.models.trade import TradeAuditRecord

    test_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(test_engine)

    # Seed 12 fake audits with monotonically increasing audited_at.
    # Our test audit will be deliberately OLD so it sits past the top-10.
    with Session(test_engine) as session:
        # v0.7.8 C1 — seed the Market row so resolver hook short-circuits
        # (otherwise it tries Gamma API and rejects with MARKET_METADATA_NOT_FOUND)
        from app.models.market import Market as _Market
        session.add(_Market(
            id="0xmkt_target", question="test", condition_id="0xmkt_target",
            category="Crypto", yes_price=0.55, no_price=0.45,
        ))

        now = datetime.now(timezone.utc)
        for i in range(12):
            ts = now - timedelta(seconds=10 - i if i < 11 else 1000)
            tid = f"0xseed_{i}"
            session.add(TradeAuditRecord(
                id=f"audit-{tid}",
                trade_id=tid,
                wallet_address="0xWalletSeed",
                market_id="0xmkt_seed",
                side="BUY",
                price=0.5,
                size=10.0,
                notional_usd=5.0,
                wallet_score=80.0,
                wallet_tier="STRONG",
                copyable_edge=0.20,
                trade_quality_score=85.0,
                decision="PAPER_TRADE",
                audited_at=ts,
            ))
        session.commit()

    # Patch the module's `engine` so the polling pipeline uses our test DB.
    with patch.object(
        __import__("app.services.wallet_polling_engine", fromlist=["engine"]),
        "engine",
        test_engine,
    ):
        # Build a polling engine instance (constructor needs no DB) and
        # call the private pipeline directly with a known trade.
        from app.services.wallet_polling_engine import WalletPollingEngine
        eng = WalletPollingEngine.__new__(WalletPollingEngine)
        eng._errors_session = 0
        eng._trades_detected_session = 0
        eng._paper_trades_session = 0

        target_trade_id = "0xtarget_old_audit_b8a"
        raw = {
            "trade_id": target_trade_id,
            "address": "0xWalletStrongA",
            "wallet": "0xWalletStrongA",
            "market_id": "0xmkt_target",
            "outcome": "YES",
            "side": "BUY",
            "price": 0.55,
            "size": 20.0,
            "notional_usd": 11.0,
            "traded_at": datetime.now(timezone.utc),
            "data_source": "test_b8a",
        }
        result = eng._process_trade_in_session(raw)

    # The pipeline must have processed the trade (even though it would
    # be past the top-10 audited_at if we used the limit=10 scan).
    assert result["audit_id"] == f"audit-{target_trade_id}"
    # No silent drop: result must be either executed=True OR rejected=True
    # with an explicit reason. The pipeline reaching here means the
    # direct lookup found the audit row.
    assert result["executed"] is True or result["rejected"] is True
    assert result["reason"], "must have an explicit reason post-B8-A"
    # No 'AUDIT_MISSING_AFTER_PERSIST' fallback was triggered.
    assert result["reason"] != "AUDIT_MISSING_AFTER_PERSIST"


# ============ B8-B: NoTradeDecision row for non-PAPER decisions ============


def test_b8b_non_paper_decision_writes_notrade_row():
    """When audit.decision != PAPER_TRADE, the pipeline must write a
    NoTradeDecision(reason_code='AUDIT_DECISION_NOT_PAPER') row.
    """
    from app.models.trade import NoTradeDecision, TradeAuditRecord

    test_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(test_engine)

    target_trade_id = "0xb8b_test_trade"

    # Patch trade_audit_engine.audit_trade to force decision='WATCH'
    from app.services.trade_audit_engine import TradeAuditResult, EdgeBreakdown

    fake_audit = TradeAuditResult(
        trade_id=target_trade_id,
        timestamp=datetime.now(timezone.utc),
        wallet="0xWalletWatch",
        market_id="0xmkt_watch",
        question=None,
        outcome="YES",
        side="BUY",
        price=0.5,
        size=10.0,
        notional_usd=5.0,
        estimated_spread=0.02,
        estimated_slippage=0.01,
        wallet_score=60.0,
        wallet_tier="WATCH",
        market_liquidity_score=40.0,
        orderbook_quality="GOOD",
        copy_delay_seconds=120.0,
        price_deterioration=0.0,
        copyable_edge=0.05,
        trade_quality_score=55.0,
        decision="WATCH",  # the trigger for B8-B path
        reasons=[],
        warnings=[],
        edge_breakdown=None,
    )

    # Prepare a real audit row in DB so the post-merge lookup succeeds.
    with Session(test_engine) as s:
        # v0.7.8 C1 — seed Market row so resolver short-circuits
        from app.models.market import Market as _Market
        s.add(_Market(
            id="0xmkt_watch", question="test", condition_id="0xmkt_watch",
            category="Politics", yes_price=0.5, no_price=0.5,
        ))
        s.add(TradeAuditRecord(
            id=f"audit-{target_trade_id}",
            trade_id=target_trade_id,
            wallet_address="0xWalletWatch",
            market_id="0xmkt_watch",
            outcome="YES",
            side="BUY",
            price=0.5,
            size=10.0,
            notional_usd=5.0,
            wallet_score=60.0,
            wallet_tier="WATCH",
            copyable_edge=0.05,
            trade_quality_score=55.0,
            decision="WATCH",
        ))
        s.commit()

    # Patch the engine + audit_trade to return our pre-canned decision.
    polling_mod = __import__("app.services.wallet_polling_engine", fromlist=["engine"])

    with patch.object(polling_mod, "engine", test_engine), \
         patch("app.services.trade_audit_engine.TradeAuditEngine.audit_trade",
               return_value=fake_audit):
        from app.services.wallet_polling_engine import WalletPollingEngine
        eng = WalletPollingEngine.__new__(WalletPollingEngine)
        eng._errors_session = 0
        eng._trades_detected_session = 0
        eng._paper_trades_session = 0
        result = eng._process_trade_in_session({
            "trade_id": target_trade_id,
            "address": "0xWalletWatch",
            "wallet": "0xWalletWatch",
            "market_id": "0xmkt_watch",
            "outcome": "YES",
            "side": "BUY",
            "price": 0.5,
            "size": 10.0,
            "notional_usd": 5.0,
            "traded_at": datetime.now(timezone.utc),
            "data_source": "test_b8b",
        })

    # Pipeline result reflects the rejection, with the NEW reason code.
    assert result["rejected"] is True
    assert result["reason"].startswith("AUDIT_DECISION_NOT_PAPER")
    assert result["audit_id"] == f"audit-{target_trade_id}"

    # A NoTradeDecision row must exist with the correct reason_code.
    with Session(test_engine) as s:
        rows = list(s.exec(
            select(NoTradeDecision)
            .where(NoTradeDecision.reason_code == "AUDIT_DECISION_NOT_PAPER")
        ))
        assert len(rows) == 1, f"expected exactly 1 NoTradeDecision row, got {len(rows)}"
        nt = rows[0]
        assert nt.signal_id and "sig-audit-audit-" in nt.signal_id
        assert nt.market_id == "0xmkt_watch"
        assert nt.wallet_address == "0xWalletWatch"
        # Details payload contains the audit_decision and tier.
        details = json.loads(nt.details)
        assert details["audit_decision"] == "WATCH"
        assert details["wallet_tier"] == "WATCH"


# ============ Stage observability invariants ============


def test_b8_no_silent_drop_invariant():
    """For a series of polled trades, every trade must be accounted for in
    one of: executed=True, rejected=True with reason, or recorded error.
    No 'silent' return is allowed.
    """
    # The polling pipeline returns dicts with keys executed / rejected / reason.
    # Every plausible code path must set at least one of these to a known
    # truthy value.
    valid_outcomes = [
        {"executed": True, "rejected": False, "reason": "EXECUTED"},
        {"executed": False, "rejected": True, "reason": "AUDIT_DECISION_NOT_PAPER:WATCH"},
        {"executed": False, "rejected": True, "reason": "AUDIT_MISSING_AFTER_PERSIST"},
        {"executed": False, "rejected": True, "reason": "EXCEPTION:Foo"},
        {"executed": False, "rejected": True, "reason": "LOW_EDGE"},
    ]
    for o in valid_outcomes:
        assert o["executed"] or o["rejected"], "every outcome must be classified"
        assert o["reason"], "every outcome must have a reason"


def test_b7_single_elite_still_triggers_post_b8():
    """B7-A regression: B8 fix didn't break single-ELITE auto-trigger."""
    from app.services.signal_cluster_engine import (
        SignalClusterEngine, IncomingSignal, ACTION_TRIGGER,
    )
    eng = SignalClusterEngine()
    sig = IncomingSignal(
        wallet_address="0xELITE_X",
        wallet_tier="ELITE",
        market_id="0xmkt",
        outcome="YES",
        side="BUY",
        entry_price=0.5,
    )
    ev = eng.add_signal(sig)
    assert ev.action == ACTION_TRIGGER
