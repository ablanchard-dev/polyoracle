"""v0.5.8.1 — risk engine integration tests (Gate 4).

These tests drive the *full* PaperTradingEngine.maybe_auto_trade path
against a fresh in-memory engine, with the risk-mode registry actually
loaded. Synthetic but exhaustive.

Scenarios:
  1. LOW_CONFIDENCE       — wallet sample 5 → refusal
  2. WIDE_SPREAD          — spread 8% → refusal
  3. BAD_ORDERBOOK        — UNTRADABLE quality → refusal
  4. NO_COPYABLE_EDGE     — price_deterioration 30% → refusal
  5. LATE_ENTRY           — copy_delay 30 min → refusal
  6. TOO_MUCH_EXPOSURE    — 16th position with cap 15 → refusal
  7. KILL_SWITCH active   → refusal
  8. WALLET_NOT_RELIABLE  — WATCH tier in AGGRESSIVE → refusal
  9. ACCEPT_PATH          — clean ELITE wallet trade → execution
 10. DEDUPE               — duplicate signal → 1 paper trade only
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

os.environ.pop("DATABASE_URL", None)

from app.models.bot import BotLoopState, BotState, RiskModeState  # noqa: E402
from app.models.market import Market  # noqa: E402
from app.models.signal import Signal  # noqa: E402
from app.models.trade import PaperTrade  # noqa: E402
from app.models.wallet import MarketFirstWalletRecord, Wallet  # noqa: E402
from app.services.paper_trading_engine import PaperTradingEngine  # noqa: E402


@pytest.fixture()
def session(tmp_path: Path) -> Session:  # type: ignore[misc]
    db_path = tmp_path / "ri.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        # Seed minimum state: bot in PAPER mode, AGGRESSIVE risk profile.
        s.add(BotLoopState(id=1, mode="PAPER", running=True))
        s.add(BotState(id=1, mode="PAPER", paper_capital=10_000.0))
        s.add(RiskModeState(id=1, mode="AGGRESSIVE"))
        # Seed a market with usable liquidity.
        s.add(Market(
            id="0xmarket",
            question="Test",
            yes_price=0.50,
            no_price=0.50,
            volume_24h=10_000.0,
            liquidity=5_000.0,
            spread=0.02,
            updated_at=datetime.now(UTC),
        ))
        # Seed an ELITE wallet record.
        s.add(MarketFirstWalletRecord(
            address="0xelite",
            tier="ELITE",
            candidate_status="ELITE",
            win_rate_confidence="HIGH",
            resolved_markets_traded=120,
            market_first_score=92.0,
        ))
        s.add(Wallet(address="0xelite", tier="ELITE", score=92.0))
        s.commit()
        yield s


def _signal(market_id: str = "0xmarket", decision: str = "PAPER_TRADE", score: float = 80.0,
            wallets: str = '["0xelite"]', sid: str = "sig-test-1") -> Signal:
    return Signal(
        id=sid, market_id=market_id, decision=decision, score=score,
        confidence=0.85, copyable_edge=0.10, status="active",
        wallets=wallets, proposed_size_usd=50.0, outcome="Yes",
        generated_at=datetime.now(UTC),
    )


def _ctx(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "spread_pct": 0.015,
        "liquidity": 5_000.0,
        "copy_delay_seconds": 30.0,
        "price_deterioration": 0.01,
        "confidence_score": 85.0,
        "copyable_edge_score": 60.0,
        "wallet_tier": "ELITE",
        "candidate_status": "ELITE",
        "win_rate_confidence": "HIGH",
        "market_sample_size": 120,
        "orderbook_quality": "ACCEPTABLE",
        "liquidity_score": 50.0,
        "price": 0.40,
        "side": "BUY",
        "wallet": "0xelite",
        "signal_score": 80.0,
        "signal_id": "sig-test-1",
        "market_id": "0xmarket",
    }
    base.update(overrides)
    return base


def test_1_low_confidence_refuses(session: Session) -> None:
    res = PaperTradingEngine(session).maybe_auto_trade(
        _signal(),
        _ctx(win_rate_confidence="INSUFFICIENT_DATA", market_sample_size=5,
              candidate_status="CANDIDATE_ELITE"),
    )
    assert not res.get("executed")
    assert res.get("reason_code") in {"LOW_CONFIDENCE", "WALLET_NOT_RELIABLE", "INVALID_SAMPLE_SIZE", "INSUFFICIENT_DATA", "INSUFFICIENT_SAMPLE"}


def test_2_wide_spread_refuses(session: Session) -> None:
    res = PaperTradingEngine(session).maybe_auto_trade(_signal(), _ctx(spread_pct=0.08))
    assert not res.get("executed")
    assert res.get("reason_code") in {"WIDE_SPREAD", "WIDE_BIDASK_SPREAD"}


def test_3_bad_orderbook_refuses(session: Session) -> None:
    res = PaperTradingEngine(session).maybe_auto_trade(_signal(), _ctx(orderbook_quality="UNTRADABLE"))
    assert not res.get("executed")
    assert res.get("reason_code") in {"BAD_ORDERBOOK", "ORDERBOOK_BAD"}


def test_4_no_copyable_edge_refuses(session: Session) -> None:
    res = PaperTradingEngine(session).maybe_auto_trade(_signal(),
                                                       _ctx(price_deterioration=0.30, copyable_edge_score=10.0))
    assert not res.get("executed")
    assert res.get("reason_code") in {"NO_COPYABLE_EDGE", "LATE_ENTRY", "PRICE_MOVED_TOO_FAR"}


def test_5_late_entry_refuses(session: Session) -> None:
    res = PaperTradingEngine(session).maybe_auto_trade(_signal(), _ctx(copy_delay_seconds=1800))
    assert not res.get("executed")
    assert res.get("reason_code") in {"LATE_ENTRY", "COPY_DELAY_TOO_HIGH"}


def test_6_too_much_exposure_refuses(session: Session) -> None:
    # Pre-seed 15 open positions to cap.
    eng = PaperTradingEngine(session)
    for k in range(15):
        eng.open_paper_position(
            market_id=f"0xm{k}", outcome="Yes", side="BUY",
            quantity=10.0, price=0.5, spread=0.02,
            signal_id=f"sig-{k}", wallet_address="0xelite", notional_usd=5.0, auto=True,
            allocator_checked=True,
        )
    res = eng.maybe_auto_trade(_signal(sid="sig-test-overcap", market_id="0xmnew"),
                               _ctx(market_id="0xmnew"))
    assert not res.get("executed")
    assert res.get("reason_code") in {"TOO_MANY_OPEN_POSITIONS", "TOO_MUCH_EXPOSURE", "EXPOSURE_CAP"}


def test_7_kill_switch_refuses(session: Session) -> None:
    state = session.get(BotLoopState, 1)
    state.kill_switch_active = True
    session.add(state)
    session.commit()
    res = PaperTradingEngine(session).maybe_auto_trade(_signal(), _ctx())
    assert not res.get("executed")
    assert res.get("reason_code") in {"KILL_SWITCH", "BOT_KILLED", "BOT_NOT_PAPER", "KILL_SWITCH_ACTIVE"}


def test_8_wallet_not_reliable_refuses(session: Session) -> None:
    """A wallet whose candidate_status is DROPPED → AGGRESSIVE refuses."""
    res = PaperTradingEngine(session).maybe_auto_trade(
        _signal(),
        _ctx(candidate_status="DROPPED", wallet_tier="DROPPED"),
    )
    assert not res.get("executed")
    assert res.get("reason_code") in {"WALLET_NOT_RELIABLE", "WALLET_TIER_NOT_ALLOWED", "INVALID_WALLET"}


def test_9_clean_elite_executes(session: Session) -> None:
    """ELITE wallet, ACCEPTABLE orderbook, fast copy, low spread → execute."""
    res = PaperTradingEngine(session).maybe_auto_trade(_signal(), _ctx())
    assert res.get("executed"), f"expected execution, got: {res}"
    assert res.get("size_usd") is not None
    open_count = sum(1 for t in session.query(PaperTrade).all() if t.status == "open")
    assert open_count >= 1


def test_10_dedupe_signal(session: Session) -> None:
    """Same signal_id replayed → still only 1 paper trade open."""
    eng = PaperTradingEngine(session)
    sig = _signal(sid="sig-dedupe-1")
    res1 = eng.maybe_auto_trade(sig, _ctx(signal_id="sig-dedupe-1"))
    res2 = eng.maybe_auto_trade(sig, _ctx(signal_id="sig-dedupe-1"))
    assert res1.get("executed")
    assert not res2.get("executed")
    assert res2.get("reason_code") in {"DUPLICATE_SIGNAL", "POSITION_ALREADY_OPEN", "DEDUPE", "TOO_MUCH_EXPOSURE"}
    assert "duplicate" in (res2.get("reason") or "").lower()
    open_count = sum(1 for t in session.query(PaperTrade).all() if t.status == "open" and t.signal_id == "sig-dedupe-1")
    assert open_count == 1
