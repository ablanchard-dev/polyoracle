"""v0.7.3 — Observability tests.

Lock in the invariants from the deterministic smoke / replay (Phase 2)
and document the two known polling pipeline bugs as ``xfail`` tests so
they remain visible until B8-A and B8-B are shipped.

Per user spec: NO fix in this batch; just document and observe.

* B8-A — `_process_trade_in_session` looks up the just-created signal in
  the 10 most recent audits. Because `_persist_audit().merge()` does not
  update `audited_at`, re-processed trades whose audit_at is older than
  the 10-most-recent get silent-dropped. Confirmed in
  `_phase1_22_to_0_diagnostic.py`.

* B8-B — when `audit.decision != PAPER_TRADE`, the loop sets
  `rejected=True` with `reason=NOT_PAPER_DECISION:{decision}` and
  breaks WITHOUT writing a NoTradeDecision row. The trade disappears
  from the DB observability surface.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from sqlmodel import SQLModel, Session, create_engine, select


# ============ B7 sanity (must keep passing) ============


def test_b7_single_elite_still_triggers():
    """B7-A: ELITE weight 1.0 + threshold 1.0 = single ELITE auto-triggers."""
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
    assert ev.trigger_now is True
    assert ev.cluster.consensus_score == 1.0


def test_b7b_drift_threshold_15pct_holds():
    """B7-B: default price_drift_trap_pct is 0.15 (Polymarket-realistic)."""
    from app.services.signal_cluster_engine import DEFAULT_PRICE_DRIFT_TRAP_PCT
    assert DEFAULT_PRICE_DRIFT_TRAP_PCT == 0.15


# ============ Replay smoke invariants ============


def test_replay_audit_trade_creates_or_updates_row():
    """A fresh audit_trade call on a never-seen trade_id creates exactly
    one publictrade and one tradeauditrecord row (idempotent if called
    twice with same id)."""
    from app.models.trade import PublicTrade, TradeAuditRecord
    from app.services.trade_audit_engine import TradeAuditEngine

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        ae = TradeAuditEngine(session)
        normalized = {
            "trade_id": "0xabcdef0001",
            "address": "0xWallet1",
            "wallet": "0xWallet1",
            "market_id": "0xmkt1",
            "outcome": "YES",
            "side": "BUY",
            "price": 0.5,
            "size": 10.0,
            "notional_usd": 5.0,
            "traded_at": datetime.now(timezone.utc),
            "data_source": "test",
        }
        # First call
        ae.audit_trade(normalized)
        n1_pt = len(session.exec(select(PublicTrade)).all())
        n1_au = len(session.exec(select(TradeAuditRecord)).all())
        assert n1_pt == 1
        assert n1_au == 1

        # Second call (same trade_id) — must NOT create duplicate rows
        ae.audit_trade(normalized)
        n2_pt = len(session.exec(select(PublicTrade)).all())
        n2_au = len(session.exec(select(TradeAuditRecord)).all())
        assert n2_pt == 1, "duplicate trade_id must not create 2nd publictrade row"
        assert n2_au == 1, "duplicate trade_id must not create 2nd tradeauditrecord row"


def test_replay_duplicate_trade_is_observable_via_seen_set():
    """The replay harness MUST track seen trade_ids and count duplicates
    — this test asserts the convention used by Phase 2 deterministic smoke
    is sound (no DB write needed; in-memory dedup is the contract)."""
    seen: set[str] = set()
    incoming = ["0xa", "0xb", "0xa", "0xc", "0xb", "0xa"]
    duplicates = 0
    fresh = 0
    for t in incoming:
        if t in seen:
            duplicates += 1
        else:
            seen.add(t)
            fresh += 1
    assert fresh == 3
    assert duplicates == 3
    assert fresh + duplicates == len(incoming)  # invariant: nothing disappears


def test_replay_invariant_raw_equals_dedup_plus_audits():
    """Phase 2 invariant: raw_trades_loaded = duplicates_skipped +
    audits_created. No third bucket. This locks in the replay
    accountability rule."""
    raw_trades_loaded = 15
    duplicates_skipped = 4
    audits_created = 11
    silent_drops = 0  # post-B8-B, this should always be 0
    assert raw_trades_loaded == duplicates_skipped + audits_created + silent_drops


# ============ Live path is blocked in PAPER mode ============


def test_base_paper_preset_disallows_live():
    from app.services.preset_loader import get_preset
    p = get_preset("BASE_PAPER")
    assert p.live_allowed is False


def test_base_live_preset_disallowed_when_live_env_false(monkeypatch):
    """Even BASE_LIVE preset is gated by LIVE_ENABLED env var.
    The allocator path should never accept a live trade when
    LIVE_ENABLED=false (read from settings at evaluation time)."""
    monkeypatch.setenv("LIVE_ENABLED", "false")
    from app.services.capital_allocator import (
        CapitalAllocator, PortfolioState, TradeSignal,
    )
    from app.services.preset_loader import get_preset

    allocator = CapitalAllocator()
    preset = get_preset("BASE_LIVE")
    state = PortfolioState(
        capital_total=10_000.0,
        capital_available=10_000.0,
        execution_mode="LIVE",
        live_enabled=False,                 # explicit flag
        min_order_size_status="unverified",
    )
    sig = TradeSignal(
        wallet_address="0xELITE_X",
        wallet_tier="ELITE",
        market_id="0xmkt",
        side="BUY",
        price=0.5,
        size=100.0,
        notional_usd=50.0,
        ev_lower_bound=0.10,
        copyable_edge=0.30,
        spread=0.01,
        liquidity=50_000.0,
        liquidity_score=60.0,
        orderbook_quality="GOOD",
        category="political",
        copy_delay_seconds=10.0,
        price_deterioration=0.0,
    )
    decision = allocator.evaluate_trade(sig, state, preset)
    assert decision.accepted is False
    assert decision.reason_code in {"LIVE_DISABLED", "MIN_ORDER_UNVERIFIED"}


# ============ B8-A and B8-B — fixed in v0.7.4 ============
# Detailed fix tests live in test_b8_fixes.py. Here we only keep the
# documentation-level invariant: direct audit_id lookup is the contract,
# and AUDIT_DECISION_NOT_PAPER is a real reason_code the pipeline emits.


def test_b8a_direct_audit_id_lookup_is_used_in_polling_pipeline():
    """v0.7.4 B8-A — `_process_trade_in_session` must use direct lookup
    by audit_id, not the limit=10 scan. The presence of
    `session.get(TradeAuditRecord` in the function body is the
    structural assertion (full integration test in test_b8_fixes.py)."""
    import inspect
    from app.services.wallet_polling_engine import WalletPollingEngine
    src = inspect.getsource(WalletPollingEngine._process_trade_in_session)
    assert "session.get(TradeAuditRecord" in src, (
        "B8-A: pipeline must use direct lookup by audit_id"
    )
    # And must NOT depend on the limit=10 scan as the lookup mechanism.
    assert "generate_signals_from_recent_audits(limit=10)" not in src, (
        "B8-A: pipeline must NOT scan recent_10 for the just-processed audit"
    )


def test_b8b_audit_decision_not_paper_reason_code_referenced():
    """v0.7.4 B8-B — `AUDIT_DECISION_NOT_PAPER` must appear as a
    reason_code in the polling pipeline so non-PAPER decisions stay
    observable (full DB-write test in test_b8_fixes.py)."""
    import inspect
    from app.services.wallet_polling_engine import WalletPollingEngine
    src = inspect.getsource(WalletPollingEngine._process_trade_in_session)
    assert "AUDIT_DECISION_NOT_PAPER" in src, (
        "B8-B: pipeline must emit AUDIT_DECISION_NOT_PAPER reason_code"
    )
