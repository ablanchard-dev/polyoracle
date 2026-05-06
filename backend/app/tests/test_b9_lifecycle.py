"""v0.7.6 B9 — paper-trade auto-close lifecycle tests.

Per spec, every status='open' papertrade must reach one of FOUR explicit
terminal states. No silent deletes. Each close records:
- holding_minutes
- capital_released (USDC)
- realized_pnl
- close_price (= exit_price)
- close_reason (exact constant)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.trade import PaperTrade


def _make_session():
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def _make_open_trade(session, *, opened_offset_hours=0, side="BUY", entry=0.5,
                     qty=10.0, notional=5.0, market_id="0xmkt", outcome="YES",
                     wallet="0xWalletA", trade_id=None):
    from uuid import uuid4
    now = datetime.utcnow()
    t = PaperTrade(
        id=trade_id or str(uuid4()),
        market_id=market_id,
        outcome=outcome,
        side=side,
        quantity=qty,
        average_price=entry,
        notional_usd=notional,
        status="open",
        opened_at=now - timedelta(hours=opened_offset_hours),
        wallet_address=wallet,
        auto=True,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _close_payload(trade: PaperTrade) -> dict:
    """close_reason convention is '<REASON>|<json_metadata>'."""
    raw = trade.close_reason or ""
    parts = raw.split("|", 1)
    return {
        "reason": parts[0],
        "metadata": json.loads(parts[1]) if len(parts) == 2 else {},
    }


# ============ 1. TTL stale auto-close ============


def test_stale_papertrade_24h_auto_closes_stale_ttl():
    from app.services.paper_trading_engine import (
        auto_close_stale_papertrades, CLOSE_REASON_STALE_TTL,
    )
    sess = _make_session()
    fresh = _make_open_trade(sess, opened_offset_hours=2)   # 2h: keep
    stale = _make_open_trade(sess, opened_offset_hours=72)  # 72h: close

    result = auto_close_stale_papertrades(sess, ttl_hours=24)
    assert result["closed"] == 1
    assert stale.id in result["trade_ids"]

    # Verify final states
    fresh_after = sess.get(PaperTrade, fresh.id)
    stale_after = sess.get(PaperTrade, stale.id)
    assert fresh_after.status == "open"
    assert stale_after.status == "closed"

    payload = _close_payload(stale_after)
    assert payload["reason"] == CLOSE_REASON_STALE_TTL
    assert payload["metadata"]["holding_minutes"] >= 60 * 71  # at least 71h
    assert payload["metadata"]["capital_released_usdc"] == stale_after.notional_usd
    assert "exit_price" in payload["metadata"]


# ============ 2. Resolved-market auto-close ============


def test_resolved_market_papertrade_auto_closes_resolved():
    """A market that settles to YES with our YES BUY position -> realized_pnl > 0."""
    from app.models.market import Market
    from app.services.paper_trading_engine import (
        auto_close_resolved_papertrades, CLOSE_REASON_RESOLVED,
    )
    sess = _make_session()
    # Seed market resolved to YES
    sess.add(Market(
        id="0xresolved_mkt",
        question="?",
        condition_id="0xresolved_mkt",
        yes_price=1.0,
        no_price=0.0,
    ))
    sess.commit()
    t = _make_open_trade(sess, market_id="0xresolved_mkt", outcome="YES",
                         side="BUY", entry=0.4, qty=10.0, notional=4.0)

    result = auto_close_resolved_papertrades(sess)
    assert result["closed"] == 1
    closed = sess.get(PaperTrade, t.id)
    assert closed.status == "closed"
    # v0.7.8 B12 — realized_pnl is NET (gross - fees). For RESOLVED close:
    # only entry_fee subtracted (Polymarket oracle settles winners gross,
    # losers worthless — no exit fee on resolved).
    gross = round((1.0 - 0.4) * 10.0, 6)  # 6.0
    assert closed.fees > 0, "B12 fees should be applied"
    assert closed.realized_pnl == round(gross - closed.fees, 6)
    payload = _close_payload(closed)
    assert payload["reason"] == CLOSE_REASON_RESOLVED
    assert payload["metadata"]["exit_price"] == 1.0
    assert payload["metadata"]["gross_pnl"] == gross


# ============ 3. Capital released on close ============


def test_close_releases_capital_and_exposure():
    """closing a trade must record capital_released_usdc = notional_usd
    so the exposure tracker can free that allocation."""
    from app.services.paper_trading_engine import (
        manual_close_papertrade, CLOSE_REASON_MANUAL,
    )
    sess = _make_session()
    t = _make_open_trade(sess, notional=12.5, entry=0.5, qty=25.0)
    closed = manual_close_papertrade(sess, t.id, exit_price=0.6)
    payload = _close_payload(closed)
    assert payload["reason"] == CLOSE_REASON_MANUAL
    assert payload["metadata"]["capital_released_usdc"] == 12.5
    # v0.7.8 B12 — realized_pnl NET. Manual close subtracts both entry_fee
    # and exit_fee (we placed an exit order, not oracle settlement).
    gross = round((0.6 - 0.5) * 25.0, 6)  # 2.5
    assert closed.fees > 0
    assert closed.realized_pnl == round(gross - closed.fees, 6)
    assert payload["metadata"]["gross_pnl"] == gross


# ============ 4. PnL history preserved ============


def test_closed_papertrade_keeps_pnl_history():
    """A closed papertrade row remains in the DB (no delete) with full
    metrics preserved. queryable for PnL reports later."""
    from app.services.paper_trading_engine import (
        auto_close_stale_papertrades,
    )
    sess = _make_session()
    t = _make_open_trade(sess, opened_offset_hours=72, entry=0.5, qty=20.0,
                         notional=10.0, side="SELL")
    # SELL, exit_price 0.4 -> profit
    auto_close_stale_papertrades(sess, ttl_hours=24)

    # Confirm the row still exists and is not deleted
    row = sess.get(PaperTrade, t.id)
    assert row is not None
    assert row.status == "closed"
    assert row.notional_usd == 10.0  # original notional preserved
    # all opening data preserved
    assert row.average_price == 0.5
    assert row.quantity == 20.0
    # closing recorded
    assert row.closed_at is not None
    assert row.realized_pnl is not None


# ============ 5. No silent delete invariant ============


def test_no_silent_delete():
    """All four close reasons must keep the row alive in the DB."""
    from app.services.paper_trading_engine import (
        auto_close_stale_papertrades,
        auto_close_resolved_papertrades,
        manual_close_papertrade,
        close_signal_invalidated,
        CLOSE_REASONS,
    )
    from app.models.market import Market

    sess = _make_session()
    # 4 open trades, one to be closed by each path
    sess.add(Market(id="0xmkt_a", question="?", condition_id="0xmkt_a",
                    yes_price=1.0, no_price=0.0))
    sess.commit()

    t_stale = _make_open_trade(sess, market_id="0xmkt_stale", opened_offset_hours=72)
    t_resolved = _make_open_trade(sess, market_id="0xmkt_a", outcome="YES")
    t_manual = _make_open_trade(sess, market_id="0xmkt_b", trade_id="trade-manual")
    t_invalidated = _make_open_trade(
        sess, market_id="0xmkt_c", wallet="0xWalletExit",
    )

    auto_close_stale_papertrades(sess, ttl_hours=24)
    auto_close_resolved_papertrades(sess)
    manual_close_papertrade(sess, "trade-manual", exit_price=0.55)
    close_signal_invalidated(
        sess, wallet_address="0xWalletExit", market_id="0xmkt_c", exit_price=0.45,
    )

    # All four rows still in DB with status='closed'
    all_rows = list(sess.exec(select(PaperTrade)))
    assert len(all_rows) == 4
    for r in all_rows:
        assert r.status == "closed"
        payload = _close_payload(r)
        assert payload["reason"] in CLOSE_REASONS, f"unknown reason: {payload['reason']}"
        # all four metric fields present
        for key in ("holding_minutes", "capital_released_usdc", "exit_price"):
            assert key in payload["metadata"], f"missing {key} in {payload}"
