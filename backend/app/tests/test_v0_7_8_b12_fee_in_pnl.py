"""v0.7.8 — B12 fee subtracted from realized_pnl.

Pre-fix: realized_pnl = (exit - entry) × qty GROSS, no fee. The B12
dynamic fee was computed for the EV-after-fee gate but NEVER applied
to the actual paper PnL. Result: paper PnL was 4-7% optimistic on
Crypto trades.

Fix: ``_close_paper_with_reason`` and ``close_paper_position`` both
subtract the B12 fee:
- entry_fee always charged
- exit_fee charged only when CLOSE_RESOLVED is False (resolution
  closes settle by oracle, no exit fee in live)
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.bot import BotState
from app.models.market import Market  # ensure Market is in metadata
from app.models.trade import PaperTrade
from app.services.fee_engine import compute_dynamic_fee
from app.services.paper_trading_engine import (
    CLOSE_REASON_MANUAL,
    CLOSE_REASON_RESOLVED,
    PaperTradingEngine,
    _b12_fee_for,
    _close_paper_with_reason,
)


def _engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(e)
    return e


def _make_open_trade(session, *, market_id="0xmkt", category="Crypto",
                     side="BUY", entry=0.40, qty=100.0):
    """Insert an open paper trade + matching market row with a known category."""
    if category is not None:
        m = Market(id=market_id, question="test", category=category)
        session.add(m)
    t = PaperTrade(
        id=f"pt-{datetime.utcnow().timestamp()}",
        market_id=market_id,
        outcome="Yes",
        side=side,
        quantity=qty,
        average_price=entry,
        slippage=0.0,
        status="open",
        opened_at=datetime.utcnow(),
        notional_usd=entry * qty,
        auto=True,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


# ---------------- 1 — entry fee always charged ----------------


def test_resolution_close_charges_entry_fee_only():
    """RESOLVED close = oracle settlement, no exit fee in live."""
    eng = _engine()
    with Session(eng) as session:
        trade = _make_open_trade(session, category="Crypto", entry=0.40, qty=100.0)
        # Crypto rate 0.072. Entry fee = 100 × 0.072 × 0.4 × 0.6 = 1.728
        expected_entry_fee = compute_dynamic_fee("Crypto", 0.40, 100.0)
        assert abs(expected_entry_fee - 1.728) < 1e-6
        closed = _close_paper_with_reason(
            session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=1.0,
        )
        gross = (1.0 - 0.40) * 100.0
        assert closed.fees == round(expected_entry_fee, 6)
        assert closed.realized_pnl == round(gross - expected_entry_fee, 6)


# ---------------- 2 — manual close charges both legs ----------------


def test_manual_close_charges_entry_and_exit_fees():
    """Manual / stale-TTL / signal-invalidated = mid-market exit. Both
    legs paid Polymarket → both fees subtracted.
    2026-05-07 — synth exit slippage shifts effective_exit < raw 0.55 for BUY,
    so exit_fee is computed on effective_exit, not raw price."""
    from app.services.paper_trading_engine import _apply_synth_exit_slippage

    eng = _engine()
    with Session(eng) as session:
        trade = _make_open_trade(session, category="Crypto", entry=0.40, qty=100.0)
        entry_fee = compute_dynamic_fee("Crypto", 0.40, 100.0)
        effective_exit, _ = _apply_synth_exit_slippage(0.55, "BUY")
        exit_fee = compute_dynamic_fee("Crypto", effective_exit, 100.0)
        closed = _close_paper_with_reason(
            session, trade, reason=CLOSE_REASON_MANUAL, exit_price=0.55,
        )
        gross = (effective_exit - 0.40) * 100.0
        assert closed.fees == round(entry_fee + exit_fee, 6)
        assert closed.realized_pnl == round(gross - entry_fee - exit_fee, 6)


# ---------------- 3 — sell side semantics ----------------


def test_sell_side_pnl_signed_correctly_with_fees():
    """SELL side: gross = entry − exit. Fees subtracted same way."""
    eng = _engine()
    with Session(eng) as session:
        trade = _make_open_trade(
            session, side="SELL", category="Politics", entry=0.60, qty=50.0,
        )
        entry_fee = compute_dynamic_fee("Politics", 0.60, 50.0)
        # SELL @ 0.60, exit at 0.30 (winning side dropped) → gross = +15
        # Politics rate 0.04: entry_fee = 50×0.04×0.6×0.4 = 0.48
        closed = _close_paper_with_reason(
            session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=0.30,
        )
        gross = (0.60 - 0.30) * 50.0
        assert closed.fees == round(entry_fee, 6)
        assert closed.realized_pnl == round(gross - entry_fee, 6)


# ---------------- 4 — _b12_fee_for fallback ----------------


def test_b12_fee_for_falls_back_to_default_when_market_missing():
    """No market row → category=None → DEFAULT_FEE_RATE 5%."""
    eng = _engine()
    with Session(eng) as session:
        # No market row inserted
        fee = _b12_fee_for(session, "0xunknown_mkt", 0.40, 100.0)
        # 100 × 0.05 × 0.4 × 0.6 = 1.2
        assert fee == pytest.approx(1.2)


def test_b12_fee_for_returns_zero_for_invalid_inputs():
    """quantity ≤ 0, price out of (0,1) → fee 0."""
    eng = _engine()
    with Session(eng) as session:
        assert _b12_fee_for(session, "0xmkt", 0.0, 100) == 0.0
        assert _b12_fee_for(session, "0xmkt", 1.0, 100) == 0.0
        assert _b12_fee_for(session, "0xmkt", 0.5, 0.0) == 0.0
        assert _b12_fee_for(session, None, 0.5, 100) == 0.0


# ---------------- 5 — geopolitics is FREE ----------------


def test_geopolitics_market_pays_zero_fee():
    """Polymarket lists Geopolitics as feeRate=0."""
    eng = _engine()
    with Session(eng) as session:
        trade = _make_open_trade(session, category="Geopolitics", entry=0.50, qty=100.0)
        closed = _close_paper_with_reason(
            session, trade, reason=CLOSE_REASON_RESOLVED, exit_price=1.0,
        )
        assert closed.fees == 0.0
        # Gross PnL preserved
        assert closed.realized_pnl == round((1.0 - 0.50) * 100.0, 6)


# ---------------- 6 — close_reason metadata includes fee fields ----------------


def test_close_reason_metadata_includes_fee_breakdown():
    """The JSON appended to close_reason must surface entry_fee,
    exit_fee, gross_pnl so downstream reports can quantify the
    real-world drag from B12.
    2026-05-07 — synth exit slippage shifts effective_exit (BUY: 0.55 → ~0.5315),
    so exit_fee and gross_pnl are computed on effective_exit, not raw price."""
    import json
    from app.services.paper_trading_engine import _apply_synth_exit_slippage

    eng = _engine()
    with Session(eng) as session:
        trade = _make_open_trade(session, category="Crypto", entry=0.40, qty=100.0)
        closed = _close_paper_with_reason(
            session, trade, reason=CLOSE_REASON_MANUAL, exit_price=0.55,
        )
        assert "|" in (closed.close_reason or "")
        reason, payload = (closed.close_reason or "").split("|", 1)
        meta = json.loads(payload)
        assert "entry_fee" in meta
        assert "exit_fee" in meta
        assert "gross_pnl" in meta
        # entry fee at p=0.40, qty 100, rate 0.072 → 1.728 (entry not slipped)
        assert meta["entry_fee"] == pytest.approx(1.728, abs=1e-3)
        # exit fee uses effective_exit (post synth slippage)
        effective_exit, _ = _apply_synth_exit_slippage(0.55, "BUY")
        expected_exit_fee = compute_dynamic_fee("Crypto", effective_exit, 100.0)
        assert meta["exit_fee"] == pytest.approx(expected_exit_fee, abs=1e-3)
        # gross PnL = (effective_exit - 0.40) × 100 (post-slippage)
        expected_gross = (effective_exit - 0.40) * 100.0
        assert meta["gross_pnl"] == pytest.approx(expected_gross, abs=1e-3)
        assert meta["exit_slippage"] > 0
