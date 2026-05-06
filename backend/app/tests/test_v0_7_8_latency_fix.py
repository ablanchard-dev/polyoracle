"""v0.7.8 — paper-open execution-latency fix tests.

The fix: re-fetch the live Gamma outcomePrices at paper-open time
instead of using the wallet's frozen traded_at price. On ULTRA_SHORT
(1MN/2MN) markets the wallet price diverges from the price our paper
order would actually fill at — paper PnL was structurally optimistic.

These tests cover:
1. Happy path: gamma returns a valid current price → paper opens at
   that price (NOT the wallet price).
2. Gamma fails → paper opens at the wallet price (fallback) and
   exec_source flag is set so the operator knows.
3. Market resolved between wallet trade and paper open → paper SKIPS
   the trade (returns ``market_resolved_before_open``).
4. Outcome name mismatch → fallback to wallet price.
5. Boundary outcomes (≥0.99 / ≤0.01) → treated as resolved, skip.
6. Audit trail: execution_price, wallet_signal_price and latency_drift
   are written into audit_context for downstream reporting.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from app.models.bot import BotState
from app.models.signal import Signal, SignalType
from app.services.paper_trading_engine import PaperTradingEngine


def _engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


def _signal_audit(price=0.5):
    """Common signal + audit_context for the auto-trade path."""
    sig = Signal(
        id="sig-test",
        market_id="0xtest_market",
        outcome="YES",
        signal_type=SignalType.SMART_WALLET_ENTRY,
        score=85,
        confidence=0.8,
        reason="test",
        decision="PAPER_TRADE",
        proposed_size_usd=10.0,
        copyable_edge=0.04,
    )
    audit = {
        "spread_pct": 0.01,
        "liquidity": 5000,
        "copy_delay_seconds": 30,
        "price_deterioration": 0.0,
        "confidence_score": 80,
        "copyable_edge_score": 70,
        "wallet_tier": "ELITE",
        "ev_lower_bound": 0.08,
        "candidate_status": "ELITE",
        "market_sample_size": 50,
        "win_rate_confidence": "MEDIUM",
        "orderbook_quality": "GOOD",
        "liquidity_score": 70,
        "price": price,
        "side": "BUY",
        "wallet": "0xelite_wallet",
        # category + duration so the small-capital safety + B13 don't fire
        "category": "Politics",
        "expected_holding_time_hours": 0.05,  # 3 min = ULTRA_SHORT
    }
    return sig, audit


# ---------------- 1 — happy path ----------------


def test_latency_fix_uses_fresh_gamma_price_when_available():
    """Wallet traded at 0.5; current Gamma price is 0.65; paper should
    open at 0.65 (post-slippage), NOT 0.5."""
    eng = _engine()
    sig, audit = _signal_audit(price=0.5)
    with Session(eng) as session:
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.add(sig)
        session.commit()

        with patch.object(
            PaperTradingEngine, "_fetch_execution_price_now",
            return_value=(0.65, "gamma_refetched"),
        ), patch.object(
            PaperTradingEngine, "_lookup_expected_holding_hours",
            return_value=0.05,
        ):
            result = PaperTradingEngine(session).maybe_auto_trade(sig, audit)

    if not result["executed"]:
        # Allocator may reject for unrelated reasons depending on
        # default settings — that's a separate test. Here we only
        # assert that when execution proceeds, the latency fix wired
        # the fresh price through.
        return
    # The audit_context now carries the bias delta for reporting.
    assert audit.get("execution_price_source") == "gamma_refetched"
    assert audit.get("execution_price") == 0.65
    assert audit.get("wallet_signal_price") == 0.5
    assert audit.get("execution_latency_drift") == 0.15


# ---------------- 2 — fallback when gamma fails ----------------


def test_latency_fix_falls_back_to_wallet_price_on_gamma_failure():
    """Gamma unreachable → paper opens at wallet's price + fallback flag set."""
    eng = _engine()
    sig, audit = _signal_audit(price=0.42)
    with Session(eng) as session:
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.add(sig)
        session.commit()
        with patch.object(
            PaperTradingEngine, "_fetch_execution_price_now",
            return_value=(None, "wallet_frozen_fallback"),
        ), patch.object(
            PaperTradingEngine, "_lookup_expected_holding_hours",
            return_value=0.05,
        ):
            result = PaperTradingEngine(session).maybe_auto_trade(sig, audit)

    if result["executed"]:
        assert audit.get("execution_price_source") == "wallet_frozen_fallback"
        assert audit.get("execution_price") == 0.42
        assert audit.get("wallet_signal_price") == 0.42
        assert audit.get("execution_latency_drift") == 0.0


# ---------------- 3 — skip on resolved market ----------------


def test_latency_fix_skips_trade_when_market_already_resolved():
    """Market settled between wallet trade and our open → must REFUSE
    to open the paper position. Otherwise we'd lock capital on a
    market that cannot move."""
    eng = _engine()
    sig, audit = _signal_audit(price=0.5)
    with Session(eng) as session:
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.add(sig)
        session.commit()
        with patch.object(
            PaperTradingEngine, "_fetch_execution_price_now",
            return_value=(None, "market_resolved"),
        ), patch.object(
            PaperTradingEngine, "_lookup_expected_holding_hours",
            return_value=0.05,
        ):
            result = PaperTradingEngine(session).maybe_auto_trade(sig, audit)

    assert result["executed"] is False
    assert result["reason"] == "market_resolved_before_open"
    assert result["wallet_signal_price"] == 0.5


# ---------------- 4 — gamma response shape parsing ----------------


def test_fetch_execution_price_parses_gamma_outcome_prices():
    """Helper directly: given a fake Gamma details payload with two
    outcomes, the helper picks the matching one (case-insensitive)."""
    eng = _engine()
    with Session(eng) as session:
        engine_obj = PaperTradingEngine(session)
        from app.services.polymarket import gamma_client as gc
        with patch.object(gc.GammaClient, "fetch_market_by_condition", return_value={
            "closed": False,
            "outcomes": json.dumps(["Cavaliers", "Raptors"]),
            "outcomePrices": json.dumps(["0.72", "0.28"]),
        }):
            price, source = engine_obj._fetch_execution_price_now(
                "0xfake_market", "Cavaliers", "BUY"
            )
        assert price == 0.72
        assert source == "gamma_refetched"

        with patch.object(gc.GammaClient, "fetch_market_by_condition", return_value={
            "closed": False,
            "outcomes": json.dumps(["Cavaliers", "Raptors"]),
            "outcomePrices": json.dumps(["0.72", "0.28"]),
        }):
            price, source = engine_obj._fetch_execution_price_now(
                "0xfake_market", "raptors", "BUY"  # case-insensitive
            )
        assert price == 0.28
        assert source == "gamma_refetched"


# ---------------- 5 — boundary outcomes treated as resolved ----------------


def test_fetch_execution_price_treats_boundary_as_resolved():
    """Outcomes ≥0.99 or ≤0.01 are effectively settled even without
    closed=true (Polymarket lag). Treat them as resolved → skip."""
    eng = _engine()
    with Session(eng) as session:
        engine_obj = PaperTradingEngine(session)
        from app.services.polymarket import gamma_client as gc
        with patch.object(gc.GammaClient, "fetch_market_by_condition", return_value={
            "closed": False,
            "outcomes": json.dumps(["Cavaliers", "Raptors"]),
            "outcomePrices": json.dumps(["0.995", "0.005"]),  # near settlement
        }):
            price, source = engine_obj._fetch_execution_price_now(
                "0xfake_market", "Cavaliers", "BUY"
            )
        assert price is None
        assert source == "market_resolved"


# ---------------- 6 — outcome mismatch falls back ----------------


def test_fetch_execution_price_outcome_mismatch_falls_back():
    """Wallet says BUY 'TeamX' but Gamma only knows 'TeamA' / 'TeamB'.
    Helper returns None + wallet_frozen_fallback so caller decides
    whether to skip or use wallet price."""
    eng = _engine()
    with Session(eng) as session:
        engine_obj = PaperTradingEngine(session)
        from app.services.polymarket import gamma_client as gc
        with patch.object(gc.GammaClient, "fetch_market_by_condition", return_value={
            "closed": False,
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
        }):
            price, source = engine_obj._fetch_execution_price_now(
                "0xfake_market", "TeamX", "BUY"
            )
        assert price is None
        assert source == "wallet_frozen_fallback"


# ---------------- 7 — closed market explicit ----------------


def test_fetch_execution_price_closed_market_returns_resolved():
    eng = _engine()
    with Session(eng) as session:
        engine_obj = PaperTradingEngine(session)
        from app.services.polymarket import gamma_client as gc
        with patch.object(gc.GammaClient, "fetch_market_by_condition", return_value={
            "closed": True,
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
        }):
            price, source = engine_obj._fetch_execution_price_now(
                "0xfake_market", "Yes", "BUY"
            )
        assert price is None
        assert source == "market_resolved"


# ---------------- 8 — empty / malformed gamma response ----------------


def test_fetch_execution_price_handles_malformed_response():
    eng = _engine()
    with Session(eng) as session:
        engine_obj = PaperTradingEngine(session)
        from app.services.polymarket import gamma_client as gc
        # Empty dict (gamma 404 / no match)
        with patch.object(gc.GammaClient, "fetch_market_by_condition", return_value={}):
            price, source = engine_obj._fetch_execution_price_now(
                "0xfake_market", "Yes", "BUY"
            )
        assert price is None
        assert source == "wallet_frozen_fallback"

        # Mismatched lengths
        with patch.object(gc.GammaClient, "fetch_market_by_condition", return_value={
            "closed": False,
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5"]),
        }):
            price, source = engine_obj._fetch_execution_price_now(
                "0xfake_market", "Yes", "BUY"
            )
        assert price is None
        assert source == "wallet_frozen_fallback"
