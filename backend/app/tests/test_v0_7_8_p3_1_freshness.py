"""v0.7.8 P3.1 — Backfill / freshness eligibility tests.

Per spec :
- Audit row PRESERVED for backfill (data signal stays).
- Paper trade BLOCKED for backfill with reason STALE_SIGNAL_BACKFILL.
- crypto_5min uses stricter 90s threshold (vs 300s default).
- Every blocked trade writes a NoTradeDecision row (no silent drop).
- FRESH_POLLING flows through to allocator unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.market import Market
from app.models.trade import NoTradeDecision, TradeAuditRecord
from app.services.wallet_polling_engine import (
    BACKFILL_LOOKBACK_S,
    PAPER_ELIGIBLE_MAX_AGE_CRYPTO_5MIN_S,
    PAPER_ELIGIBLE_MAX_AGE_S,
    POLLING_SOURCE_FRESH,
    POLLING_SOURCE_RESTART_BACKFILL,
    POLLING_SOURCE_STALE_BACKFILL,
    classify_polling_source,
)


# ============ Pure classifier tests (no DB) ============


def test_fresh_polling_classification():
    now = 1_700_000_000
    src, lag = classify_polling_source(
        traded_at_unix=now - 100, now_unix=now, is_crypto_5min=False,
    )
    assert src == POLLING_SOURCE_FRESH
    assert lag == 100


def test_restart_backfill_classification():
    now = 1_700_000_000
    src, lag = classify_polling_source(
        traded_at_unix=now - 600, now_unix=now, is_crypto_5min=False,
    )
    assert src == POLLING_SOURCE_RESTART_BACKFILL
    assert lag == 600


def test_stale_backfill_classification():
    """When poll_lag exceeds BACKFILL_LOOKBACK_S (clamp leak case)."""
    now = 1_700_000_000
    src, lag = classify_polling_source(
        traded_at_unix=now - (BACKFILL_LOOKBACK_S + 100),
        now_unix=now, is_crypto_5min=False,
    )
    assert src == POLLING_SOURCE_STALE_BACKFILL
    assert lag == BACKFILL_LOOKBACK_S + 100


def test_crypto_5min_uses_stricter_threshold():
    """A 100s-old trade is FRESH for default but BACKFILL for crypto_5min."""
    now = 1_700_000_000
    src_default, _ = classify_polling_source(
        traded_at_unix=now - 100, now_unix=now, is_crypto_5min=False,
    )
    src_crypto, _ = classify_polling_source(
        traded_at_unix=now - 100, now_unix=now, is_crypto_5min=True,
    )
    assert src_default == POLLING_SOURCE_FRESH
    assert src_crypto == POLLING_SOURCE_RESTART_BACKFILL


def test_threshold_constants_match_spec():
    assert PAPER_ELIGIBLE_MAX_AGE_S == 300
    assert PAPER_ELIGIBLE_MAX_AGE_CRYPTO_5MIN_S == 90
    assert BACKFILL_LOOKBACK_S >= 1800


# ============ Pipeline integration tests ============


def _make_engine():
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_market(sess: Session, *, market_id: str, category: str, exp_min: float | None):
    sess.add(Market(
        id=market_id, question="?", category=category,
        raw_slug="test", deadline=datetime.now(timezone.utc),
        expected_resolution_minutes=exp_min,
        duration_bucket="MEDIUM",
        market_speed_class="MEDIUM",
    ))
    sess.commit()


def _make_polling_engine(test_engine):
    """Build a WalletPollingEngine bypassing __init__ (no live config)."""
    from app.services.wallet_polling_engine import WalletPollingEngine
    eng = WalletPollingEngine.__new__(WalletPollingEngine)
    eng._errors_session = 0
    eng._trades_detected_session = 0
    eng._paper_trades_session = 0
    return eng


def test_backfill_blocks_paper_with_stale_signal_reason():
    """A trade with poll_lag > 300s gets STALE_SIGNAL_BACKFILL reject,
    audit row IS still created, NoTradeDecision IS written."""
    test_engine = _make_engine()
    polling_mod = __import__("app.services.wallet_polling_engine", fromlist=["engine"])

    with Session(test_engine) as setup:
        _seed_market(setup, market_id="0xmkt_b", category="Politics", exp_min=120)

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    raw = {
        "trade_id": "0xtest_backfill",
        "address": "0xWalletA",
        "wallet": "0xWalletA",
        "market_id": "0xmkt_b",
        "outcome": "YES",
        "side": "BUY",
        "price": 0.5,
        "size": 10.0,
        "notional_usd": 5.0,
        # v0.7.8 P3.1 BUGFIX — gate uses audit_record.copy_delay_seconds
        # which is computed via `now - traded_at`. To simulate a 10-min-old
        # trade, traded_at must actually be 10 min in the past.
        "traded_at": now - timedelta(minutes=10),
        "data_source": "test",
        "timestamp": int((now - timedelta(minutes=10)).timestamp()),
    }
    with patch.object(polling_mod, "engine", test_engine):
        eng = _make_polling_engine(test_engine)
        result = eng._process_trade_in_session(raw)

    assert result["rejected"] is True
    assert "STALE_SIGNAL_BACKFILL" in result["reason"]
    assert "RESTART_BACKFILL" in result["reason"]

    # Audit row still created (data preserved)
    with Session(test_engine) as s:
        audit = s.get(TradeAuditRecord, "audit-0xtest_backfill")
        assert audit is not None, "audit row must be created even on backfill"
        # NoTradeDecision row written
        nt_rows = list(s.exec(
            select(NoTradeDecision).where(NoTradeDecision.reason_code == "STALE_SIGNAL_BACKFILL")
        ))
        assert len(nt_rows) == 1
        details = json.loads(nt_rows[0].details)
        assert details["polling_source"] == "RESTART_BACKFILL"
        assert details["poll_lag_seconds"] == 600
        assert details["paper_eligible_threshold_s"] == 300


def test_crypto_5min_120s_blocked_but_default_120s_allowed():
    """Same 120s old trade : crypto_5min market → blocked, Politics → fresh."""
    test_engine = _make_engine()
    polling_mod = __import__("app.services.wallet_polling_engine", fromlist=["engine"])

    with Session(test_engine) as setup:
        _seed_market(setup, market_id="0xmkt_crypto5m", category="Crypto", exp_min=5)
        _seed_market(setup, market_id="0xmkt_pol", category="Politics", exp_min=120)

    from datetime import timedelta
    now = datetime.now(timezone.utc)

    def _raw(trade_id, market_id):
        return {
            "trade_id": trade_id,
            "address": "0xW",
            "wallet": "0xW",
            "market_id": market_id,
            "outcome": "YES",
            "side": "BUY",
            "price": 0.5,
            "size": 10.0,
            "notional_usd": 5.0,
            # 120s old — gate reads via audit.copy_delay_seconds = now - traded_at
            "traded_at": now - timedelta(seconds=120),
            "data_source": "test",
            "timestamp": int((now - timedelta(seconds=120)).timestamp()),
        }

    with patch.object(polling_mod, "engine", test_engine):
        eng = _make_polling_engine(test_engine)
        # Crypto 5min: 120s > 90s threshold → blocked
        r_crypto = eng._process_trade_in_session(_raw("0xt_crypto", "0xmkt_crypto5m"))
        # Politics: 120s ≤ 300s threshold → fresh, will continue downstream
        r_pol = eng._process_trade_in_session(_raw("0xt_pol", "0xmkt_pol"))

    assert "STALE_SIGNAL_BACKFILL" in r_crypto["reason"]
    assert "STALE_SIGNAL_BACKFILL" not in r_pol["reason"], (
        f"Politics 120s should pass freshness gate, got: {r_pol['reason']}"
    )


def test_no_silent_drop_on_backfill():
    """Every backfilled trade must produce either an audit row OR an
    error row — never disappear silently."""
    test_engine = _make_engine()
    polling_mod = __import__("app.services.wallet_polling_engine", fromlist=["engine"])

    with Session(test_engine) as setup:
        _seed_market(setup, market_id="0xmkt_silent", category="Sports", exp_min=60)

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    raw = {
        "trade_id": "0xnosilent",
        "address": "0xW",
        "wallet": "0xW",
        "market_id": "0xmkt_silent",
        "outcome": "YES",
        "side": "BUY",
        "price": 0.5,
        "size": 10.0,
        "notional_usd": 5.0,
        "traded_at": now - timedelta(minutes=16),
        "data_source": "test",
        "timestamp": int((now - timedelta(minutes=16)).timestamp()),
    }
    with patch.object(polling_mod, "engine", test_engine):
        eng = _make_polling_engine(test_engine)
        result = eng._process_trade_in_session(raw)

    # Result must be classified, not silent
    assert result["audit_id"] == "audit-0xnosilent"
    assert result["executed"] is False
    assert result["rejected"] is True
    # Reason MUST be set (no empty string)
    assert result["reason"], "rejected trade must have a reason"
