"""Strict tests for the v0.4.2 WinRateEngine.

We never want a wallet's win rate to be inferred — only counted when the market
is resolved AND the wallet's net long position is on a known outcome.
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine

from app.services.win_rate_engine import (
    MarketResolution,
    WinRateEngine,
    compute_win_rate_confidence,
)


def _engine() -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_confidence_buckets_match_spec() -> None:
    assert compute_win_rate_confidence(0) == "INSUFFICIENT_DATA"
    assert compute_win_rate_confidence(9) == "INSUFFICIENT_DATA"
    assert compute_win_rate_confidence(10) == "LOW"
    assert compute_win_rate_confidence(29) == "LOW"
    assert compute_win_rate_confidence(30) == "MEDIUM"
    assert compute_win_rate_confidence(99) == "MEDIUM"
    assert compute_win_rate_confidence(100) == "HIGH"


def test_resolved_market_win_rate_only_counts_resolved_with_known_winner() -> None:
    """Three markets: two resolved, one unresolved — only resolved ones must count."""
    eng = _engine()
    with Session(eng) as session:
        engine = WinRateEngine(session)
        # Pre-cache resolutions so we don't hit the network.
        engine._resolution_cache["market-A"] = MarketResolution(
            market_id="market-A",
            resolved=True,
            winning_outcome_index=0,
            winning_outcome_name="YES",
            outcomes=["YES", "NO"],
            outcome_prices=[1.0, 0.0],
        )
        engine._resolution_cache["market-B"] = MarketResolution(
            market_id="market-B",
            resolved=True,
            winning_outcome_index=1,
            winning_outcome_name="NO",
            outcomes=["YES", "NO"],
            outcome_prices=[0.0, 1.0],
        )
        engine._resolution_cache["market-C"] = MarketResolution(
            market_id="market-C",
            resolved=False,
            winning_outcome_index=None,
            winning_outcome_name=None,
            outcomes=["YES", "NO"],
            outcome_prices=[0.5, 0.5],
        )
        trades = [
            # Wallet held YES on market-A (winning outcome) → win
            {"market_id": "market-A", "outcome": "YES", "side": "BUY", "size": 100, "price": 0.4, "data_source": "polymarket_data"},
            # Wallet held YES on market-B (losing outcome) → loss
            {"market_id": "market-B", "outcome": "YES", "side": "BUY", "size": 50, "price": 0.5, "data_source": "polymarket_data"},
            # Wallet bought + sold same outcome on market-C → unresolved, doesn't count
            {"market_id": "market-C", "outcome": "YES", "side": "BUY", "size": 100, "price": 0.5, "data_source": "polymarket_data"},
        ]
        breakdown = engine.compute_wallet_win_rate("0xtest", trades=trades)

    assert breakdown.resolved_winning_markets == 1
    assert breakdown.resolved_losing_markets == 1
    assert breakdown.unresolved_markets_count == 1
    assert breakdown.market_sample_size == 2
    assert breakdown.resolved_market_win_rate == 0.5
    assert breakdown.win_rate_confidence == "INSUFFICIENT_DATA"


def test_unknown_winning_outcome_does_not_count() -> None:
    """If we cannot infer the wallet's outcome, the market goes to ``unresolved`` —
    we never invent a win/loss."""
    eng = _engine()
    with Session(eng) as session:
        engine = WinRateEngine(session)
        engine._resolution_cache["market-X"] = MarketResolution(
            market_id="market-X",
            resolved=True,
            winning_outcome_index=0,
            winning_outcome_name="YES",
            outcomes=["YES", "NO"],
            outcome_prices=[1.0, 0.0],
        )
        # Net short / fully exited — net_size <= 0 → cannot infer outcome.
        trades = [
            {"market_id": "market-X", "outcome": "YES", "side": "BUY", "size": 100, "price": 0.4},
            {"market_id": "market-X", "outcome": "YES", "side": "SELL", "size": 100, "price": 0.6},
        ]
        breakdown = engine.compute_wallet_win_rate("0xnet0", trades=trades)
    assert breakdown.market_sample_size == 0
    assert breakdown.unresolved_markets_count == 1
    assert breakdown.resolved_market_win_rate is None
    assert breakdown.win_rate_confidence == "INSUFFICIENT_DATA"


def test_high_winrate_low_sample_emits_warning() -> None:
    eng = _engine()
    with Session(eng) as session:
        engine = WinRateEngine(session)
        # Build 5 winning markets, 0 losing → 100% on tiny sample.
        trades = []
        for idx in range(5):
            mid = f"market-{idx}"
            engine._resolution_cache[mid] = MarketResolution(
                market_id=mid,
                resolved=True,
                winning_outcome_index=0,
                winning_outcome_name="YES",
                outcomes=["YES", "NO"],
                outcome_prices=[1.0, 0.0],
            )
            trades.append({"market_id": mid, "outcome": "YES", "side": "BUY", "size": 10, "price": 0.5})
        breakdown = engine.compute_wallet_win_rate("0xhot", trades=trades)
    assert breakdown.resolved_market_win_rate == 1.0
    assert breakdown.win_rate_confidence == "INSUFFICIENT_DATA"
    assert "insufficient_resolved_market_sample" in breakdown.win_rate_warnings
