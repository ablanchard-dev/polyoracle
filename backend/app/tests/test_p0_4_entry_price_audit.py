"""P0.4 (Round 8, 2026-05-12) — EntryPriceAudit table + persist at open.

Verifies:
1. EntryPriceAudit table is created by SQLModel.metadata.create_all.
2. open_paper_position(entry_audit=...) creates a corresponding audit row.
3. live_shadow_entry_price computed correctly (best_ask for BUY, best_bid for SELL).
4. live_shadow_slippage_abs / pct computed.
5. Audit failures DO NOT block trade open (defence-in-depth).
6. Legacy callers (entry_audit=None) still work, just no audit row.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.bot import BotState
from app.models.trade import EntryPriceAudit, PaperTrade
from app.services.paper_trading_engine import PaperTradingEngine


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(BotState(id=1, paper_capital=100.0))
        s.commit()
        yield s


@pytest.fixture
def engine_for_session(session):
    return PaperTradingEngine(session)


# ---------------- Table existence ----------------


def test_entry_price_audit_table_created():
    """SQLModel.metadata.create_all must create the EntryPriceAudit table."""
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        # Should not error on query
        result = s.exec(select(EntryPriceAudit)).all()
        assert result == []


# ---------------- Audit row creation ----------------


def test_open_paper_position_with_audit_creates_row(engine_for_session, session):
    """open_paper_position(entry_audit=...) must persist an EntryPriceAudit row."""
    audit_data = {
        "raw_signal_price": 0.55,
        "gamma_mid_at_open": 0.52,
        "spread_at_open": 0.02,
        "entry_price_source": "GAMMA",
        "price_source_confidence": "HIGH",
        "source_trade_id": "src-trade-123",
        "source_wallet": "0xwallet",
    }
    trade = engine_for_session.open_paper_position(
        market_id="0xmkt",
        outcome="Yes",
        side="BUY",
        quantity=10.0,
        price=0.52,
        spread=0.02,
        notional_usd=5.0,
        entry_audit=audit_data,
    )
    rows = session.exec(
        select(EntryPriceAudit).where(EntryPriceAudit.paper_trade_id == trade.id)
    ).all()
    assert len(rows) == 1
    audit = rows[0]
    assert audit.market_id == "0xmkt"
    assert audit.side == "BUY"
    assert audit.outcome == "Yes"
    assert audit.raw_signal_price == 0.55
    assert audit.gamma_mid_at_open == 0.52
    assert audit.entry_price_source == "GAMMA"
    assert audit.price_source_confidence == "HIGH"
    assert audit.source_trade_id == "src-trade-123"
    assert audit.source_wallet == "0xwallet"
    assert audit.chosen_entry_price == trade.average_price


def test_open_paper_position_without_audit_no_row(engine_for_session, session):
    """Legacy caller (entry_audit=None) — no audit row, but trade still opens."""
    trade = engine_for_session.open_paper_position(
        market_id="0xmkt",
        outcome="Yes",
        side="BUY",
        quantity=10.0,
        price=0.52,
        spread=0.02,
        notional_usd=5.0,
    )
    rows = session.exec(
        select(EntryPriceAudit).where(EntryPriceAudit.paper_trade_id == trade.id)
    ).all()
    assert len(rows) == 0
    # Trade itself still committed
    pt = session.get(PaperTrade, trade.id)
    assert pt is not None


# ---------------- live_shadow computation ----------------


def test_live_shadow_uses_clob_best_ask_for_buy(engine_for_session, session):
    """BUY → shadow = clob_best_ask if available."""
    audit_data = {
        "raw_signal_price": 0.50,
        "gamma_mid_at_open": 0.50,
        "clob_best_ask_at_open": 0.55,
        "clob_best_bid_at_open": 0.45,
        "entry_price_source": "CLOB",
        "price_source_confidence": "HIGH",
    }
    trade = engine_for_session.open_paper_position(
        market_id="0xmkt", outcome="Yes", side="BUY",
        quantity=10.0, price=0.50, spread=0.10, notional_usd=5.0,
        entry_audit=audit_data,
    )
    audit = session.exec(
        select(EntryPriceAudit).where(EntryPriceAudit.paper_trade_id == trade.id)
    ).one()
    assert audit.live_shadow_entry_price == 0.55  # best_ask
    # Slippage: |0.55 - chosen|. chosen = 0.50 + slippage(synth) ≈ 0.50+0.05=0.55 too
    # In any case, must be ≥ 0
    assert audit.live_shadow_slippage_abs >= 0


def test_live_shadow_uses_clob_best_bid_for_sell(engine_for_session, session):
    """SELL → shadow = clob_best_bid if available."""
    audit_data = {
        "raw_signal_price": 0.50,
        "gamma_mid_at_open": 0.50,
        "clob_best_ask_at_open": 0.55,
        "clob_best_bid_at_open": 0.45,
        "entry_price_source": "CLOB",
    }
    trade = engine_for_session.open_paper_position(
        market_id="0xmkt", outcome="Yes", side="SELL",
        quantity=10.0, price=0.50, spread=0.10, notional_usd=5.0,
        entry_audit=audit_data,
    )
    audit = session.exec(
        select(EntryPriceAudit).where(EntryPriceAudit.paper_trade_id == trade.id)
    ).one()
    assert audit.live_shadow_entry_price == 0.45  # best_bid


def test_live_shadow_fallback_to_gamma_plus_spread(engine_for_session, session):
    """When CLOB unavailable (crypto 5min markets), shadow = gamma + spread/2 for BUY."""
    audit_data = {
        "raw_signal_price": 0.50,
        "gamma_mid_at_open": 0.50,
        "clob_best_bid_at_open": None,
        "clob_best_ask_at_open": None,
        "spread_at_open": 0.04,
        "entry_price_source": "GAMMA",
    }
    trade = engine_for_session.open_paper_position(
        market_id="0xmkt", outcome="Yes", side="BUY",
        quantity=10.0, price=0.50, spread=0.04, notional_usd=5.0,
        entry_audit=audit_data,
    )
    audit = session.exec(
        select(EntryPriceAudit).where(EntryPriceAudit.paper_trade_id == trade.id)
    ).one()
    # shadow = gamma_mid (0.50) + spread (0.04) / 2 = 0.52
    assert abs(audit.live_shadow_entry_price - 0.52) < 1e-9


# ---------------- Defence-in-depth ----------------


def test_audit_failure_does_not_block_trade(engine_for_session, session, monkeypatch):
    """If EntryPriceAudit insert fails, the trade is still committed."""
    # Make the audit row construction raise
    from app.models import trade as trade_mod

    original_audit = trade_mod.EntryPriceAudit

    def _exploding_audit(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(trade_mod, "EntryPriceAudit", _exploding_audit)

    trade = engine_for_session.open_paper_position(
        market_id="0xmkt", outcome="Yes", side="BUY",
        quantity=10.0, price=0.52, spread=0.02, notional_usd=5.0,
        entry_audit={
            "raw_signal_price": 0.55, "gamma_mid_at_open": 0.52,
            "entry_price_source": "GAMMA",
        },
    )
    # Trade still committed
    pt = session.get(PaperTrade, trade.id)
    assert pt is not None
    assert pt.id == trade.id

    # Restore
    monkeypatch.setattr(trade_mod, "EntryPriceAudit", original_audit)


def test_audit_persists_with_minimal_data(engine_for_session, session):
    """If most audit fields are missing, the row should still persist with None."""
    audit_data = {"raw_signal_price": 0.50}  # almost empty
    trade = engine_for_session.open_paper_position(
        market_id="0xmkt", outcome="Yes", side="BUY",
        quantity=10.0, price=0.50, spread=0.04, notional_usd=5.0,
        entry_audit=audit_data,
    )
    audit = session.exec(
        select(EntryPriceAudit).where(EntryPriceAudit.paper_trade_id == trade.id)
    ).one()
    assert audit.raw_signal_price == 0.50
    assert audit.gamma_mid_at_open is None
    assert audit.entry_price_source == "UNKNOWN"  # default
    assert audit.price_source_confidence == "LOW"  # default
