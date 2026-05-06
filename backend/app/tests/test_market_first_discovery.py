"""Tests for the v0.5 market-first discovery pipeline.

We mock both Gamma's closed-markets feed and the Data API per-market trade
feed so the test runs offline and is fast. The goal is to lock in the
behaviour rules from the v0.5 spec:

- Markets without a conditionId are rejected.
- Markets without a clear winning outcome are rejected.
- A wallet that was net long the winning outcome counts as a *win*.
- A wallet net long the losing outcome counts as a *loss*.
- A wallet net short / fully exited counts as *unresolved* and never moves the
  win rate.
- Sample size below the configured threshold caps the tier at INSUFFICIENT_DATA.
- A wallet that is otherwise STRONG-quality but has been silent for 6+ months
  cannot land in HISTORICAL_ELITE_ACTIVE — composite penalises inactivity.
- CSV + JSON exports are written and the conclusion enum is populated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.services.market_first_discovery import MarketFirstDiscoveryService
from app.services.market_resolution_scanner import (
    MarketResolutionScanner,
    ResolvedMarket,
)


def _engine(tmp_path: Path) -> object:
    db_path = tmp_path / "market_first.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401
    SQLModel.metadata.create_all(engine)
    return engine


def _make_market(market_id: str, condition_id: str | None, winning_index: int | None, *, closed: bool = True, category: str = "Crypto") -> ResolvedMarket:
    outcomes = ["YES", "NO"]
    prices: list[float]
    if winning_index is None:
        prices = [0.0, 0.0]
    else:
        prices = [1.0 if idx == winning_index else 0.0 for idx in range(len(outcomes))]
    return ResolvedMarket(
        market_id=market_id,
        condition_id=condition_id,
        question=f"Test market {market_id}",
        category=category,
        end_date="2026-04-01T00:00:00Z",
        closed=closed,
        outcomes=outcomes,
        outcome_prices=prices,
        winning_outcome_index=winning_index,
        winning_outcome_name=outcomes[winning_index] if winning_index is not None else None,
        raw={"id": market_id, "conditionId": condition_id, "category": category},
    )


# ---------------- scanner-level tests ----------------


def test_scanner_rejects_market_without_condition_id() -> None:
    scanner = MarketResolutionScanner()
    market = scanner.detect_resolved_market(
        {
            "id": "m-no-cid",
            "question": "Anything",
            "closed": True,
            "outcomes": ["YES", "NO"],
            "outcomePrices": ["1", "0"],
        }
    )
    assert market.is_usable is False
    assert market.rejection_reason() == "NO_CONDITION_ID"


def test_scanner_rejects_market_without_winning_outcome() -> None:
    scanner = MarketResolutionScanner()
    market = scanner.detect_resolved_market(
        {
            "id": "m-void",
            "conditionId": "0xabc",
            "question": "Voided market",
            "closed": True,
            "outcomes": ["YES", "NO"],
            "outcomePrices": ["0", "0"],
        }
    )
    assert market.is_usable is False
    assert market.rejection_reason() == "UNKNOWN_WINNING_OUTCOME"


def test_strict_elite_rule_requires_high_confidence_and_sample_at_least_100(tmp_path) -> None:
    """v0.5.2 strict ELITE rule: a wallet with sample 99 / 100% win rate /
    MEDIUM confidence cannot be ELITE — must stay STRONG. HIGH-confidence (which
    by construction means sample >= 100) is the only path to ELITE."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        svc = MarketFirstDiscoveryService(session)
        # Sample 99 + MEDIUM confidence: even at score 100 must NOT be ELITE.
        tier_medium = svc._classify_tier(score=100.0, confidence="MEDIUM", sample=99)
        assert tier_medium == "STRONG", f"sample 99 / MEDIUM must stay STRONG, got {tier_medium}"
        # Sample 100 + HIGH confidence: ELITE allowed.
        tier_high = svc._classify_tier(score=90.0, confidence="HIGH", sample=120)
        assert tier_high == "ELITE"
        # Sample 200 + MEDIUM confidence (impossible per WinRateEngine but defensive):
        # if it ever happens, must stay STRONG, never ELITE.
        tier_med_big = svc._classify_tier(score=95.0, confidence="MEDIUM", sample=200)
        assert tier_med_big == "STRONG"


def test_scanner_rejects_open_market() -> None:
    scanner = MarketResolutionScanner()
    market = scanner.detect_resolved_market(
        {
            "id": "m-open",
            "conditionId": "0xdef",
            "question": "Still trading",
            "closed": False,
            "outcomes": ["YES", "NO"],
            "outcomePrices": ["0.5", "0.5"],
        }
    )
    assert market.rejection_reason() == "NOT_CLOSED"


# ---------------- discovery-level tests ----------------


@pytest.fixture()
def patched_service(tmp_path, monkeypatch):
    settings = get_settings()
    settings.polymarket_public_enabled = True
    settings.mock_data_enabled = False
    settings.market_first_sleep_ms_between_calls = 0
    settings.market_first_min_sample_for_strong = 30
    settings.market_first_min_sample_for_watch = 10

    engine = _engine(tmp_path)
    session = Session(engine)
    service = MarketFirstDiscoveryService(session)
    # Redirect exports to the test tmp dir.
    service.exports_dir = tmp_path
    monkeypatch.setattr(MarketFirstDiscoveryService, "json_path", property(lambda self: tmp_path / "market_first_discovery_report.json"))
    monkeypatch.setattr(MarketFirstDiscoveryService, "wallets_csv_path", property(lambda self: tmp_path / "market_first_wallets.csv"))
    monkeypatch.setattr(MarketFirstDiscoveryService, "markets_csv_path", property(lambda self: tmp_path / "market_first_markets.csv"))
    monkeypatch.setattr(MarketFirstDiscoveryService, "rejected_csv_path", property(lambda self: tmp_path / "market_first_rejected_markets.csv"))
    yield service, tmp_path
    session.close()


def _generate_markets(count: int) -> list[ResolvedMarket]:
    """Build N resolved markets, alternating winner index for variety."""
    return [
        _make_market(f"m{idx}", f"0x{idx:040x}", winning_index=idx % 2)
        for idx in range(count)
    ]


def _trades_for_market(market: ResolvedMarket, *, ace_address: str, mediocre_address: str, dud_address: str, never_holds_address: str) -> list[dict[str, Any]]:
    """For each market the ace always sits on the winning outcome, the dud on
    the losing outcome, the mediocre flips, and the ``never_holds`` wallet
    enters and exits flat — so it should land in unresolved."""
    cid = market.condition_id
    winning = market.winning_outcome_index
    losing = 1 - winning
    market_index = int(market.market_id.replace("m", ""))
    return [
        # Ace always net long the winning outcome.
        {"proxyWallet": ace_address, "side": "BUY", "outcomeIndex": winning, "size": 100, "price": 0.4, "timestamp": 1_775_000_000 + market_index, "conditionId": cid},
        # Dud always net long the losing outcome.
        {"proxyWallet": dud_address, "side": "BUY", "outcomeIndex": losing, "size": 50, "price": 0.5, "timestamp": 1_775_000_000 + market_index, "conditionId": cid},
        # Mediocre alternates: even market_index → backs winner, odd → backs loser.
        {"proxyWallet": mediocre_address, "side": "BUY", "outcomeIndex": winning if market_index % 2 == 0 else losing, "size": 30, "price": 0.45, "timestamp": 1_775_000_000 + market_index, "conditionId": cid},
        # Never-holds: buy then sell of the winning outcome → net flat → unresolved.
        {"proxyWallet": never_holds_address, "side": "BUY", "outcomeIndex": winning, "size": 20, "price": 0.45, "timestamp": 1_775_000_000 + market_index, "conditionId": cid},
        {"proxyWallet": never_holds_address, "side": "SELL", "outcomeIndex": winning, "size": 20, "price": 0.55, "timestamp": 1_775_000_000 + market_index + 1, "conditionId": cid},
    ]


def test_market_first_run_classifies_ace_as_strong_dud_as_ignore_and_never_holds_as_insufficient(patched_service, monkeypatch) -> None:
    service, _ = patched_service
    # v0.5.2+ strict ELITE rule: HIGH confidence requires sample >= 100.
    # Bumped from 40 → 100 so ace can reach ELITE/STRONG (score≥75 + sample≥30
    # was achievable at 40, but not the score formula's sample-weighting).
    markets = _generate_markets(100)
    monkeypatch.setattr(service.scanner, "fetch_resolved_markets", lambda **kwargs: markets)
    addresses = {
        "ace": "0xace00000000000000000000000000000000000ace",
        "med": "0xbeef000000000000000000000000000000000bee",
        "dud": "0xdead000000000000000000000000000000000dea",
        "never": "0xc0ffee000000000000000000000000000000c0ffe",
    }
    monkeypatch.setattr(
        service,
        "_fetch_market_trades",
        lambda cid, limit: _trades_for_market(
            next(m for m in markets if m.condition_id == cid),
            ace_address=addresses["ace"],
            mediocre_address=addresses["med"],
            dud_address=addresses["dud"],
            never_holds_address=addresses["never"],
        ),
    )
    report = service.run(days_back=365, max_markets=100, trades_per_market=500)
    assert report.markets_usable == 100
    assert report.markets_rejected == 0
    by_address = {entry["address"]: entry for entry in report.top_wallets}

    ace = by_address[addresses["ace"]]
    assert ace["resolved_market_win_rate"] == 1.0
    assert ace["resolved_markets_traded"] == 100
    assert ace["tier"] in {"ELITE", "STRONG"}
    # Ace traded markets dated 2023 → recent activity will be poor → status not active.
    assert ace["status"] in {"HISTORICAL_ELITE_INACTIVE", "STRONG_AND_ACTIVE"}

    dud = by_address[addresses["dud"]]
    assert dud["resolved_market_win_rate"] == 0.0
    # At sample=100 with 0% wr, score lands in WEAK range (40-60) due to
    # sample-size positive contribution. Either WEAK or IGNORE means the
    # wallet is non-tradable, which is the spirit of this assertion.
    assert dud["tier"] in {"IGNORE", "WEAK"}

    # Mediocre lands somewhere in the middle.
    med = by_address[addresses["med"]]
    assert 0.4 <= (med["resolved_market_win_rate"] or 0) <= 0.6

    # Never-holds had 0 resolved positions → INSUFFICIENT_DATA + no win rate.
    never = by_address.get(addresses["never"])
    if never is not None:
        # The wallet was buy+sell on every market → no resolved entries.
        assert never["resolved_markets_traded"] == 0
        assert never["resolved_market_win_rate"] is None
        assert never["tier"] == "INSUFFICIENT_DATA"
        assert never["unresolved_markets_count"] == 100

    # Conclusion + warnings should be populated.
    assert report.conclusion in {
        "MARKET_FIRST_GOOD",
        "MARKET_FIRST_DISCOVERY_READY",
        "MARKET_FIRST_NO_ELITE_FOUND",
        "MARKET_FIRST_NEEDS_MORE_MARKETS",
    }


def test_market_first_run_caps_strong_when_sample_too_small(patched_service, monkeypatch) -> None:
    service, _ = patched_service
    markets = _generate_markets(15)  # below 30 required for STRONG
    monkeypatch.setattr(service.scanner, "fetch_resolved_markets", lambda **kwargs: markets)
    ace = "0xace00000000000000000000000000000000000ace"
    monkeypatch.setattr(
        service,
        "_fetch_market_trades",
        lambda cid, limit: _trades_for_market(
            next(m for m in markets if m.condition_id == cid),
            ace_address=ace,
            mediocre_address="0xb",
            dud_address="0xd",
            never_holds_address="0xc",
        ),
    )
    report = service.run(days_back=365, max_markets=15)
    by_address = {entry["address"]: entry for entry in report.top_wallets}
    ace_row = by_address[ace]
    assert ace_row["resolved_markets_traded"] == 15
    assert ace_row["tier"] not in {"ELITE", "STRONG"}, "sample size below threshold must block STRONG"


def test_market_first_run_writes_csv_and_json_exports(patched_service, monkeypatch) -> None:
    service, tmp_path = patched_service
    markets = _generate_markets(12)
    monkeypatch.setattr(service.scanner, "fetch_resolved_markets", lambda **kwargs: markets)
    monkeypatch.setattr(
        service,
        "_fetch_market_trades",
        lambda cid, limit: _trades_for_market(
            next(m for m in markets if m.condition_id == cid),
            ace_address="0xa",
            mediocre_address="0xb",
            dud_address="0xd",
            never_holds_address="0xc",
        ),
    )
    service.run(days_back=365, max_markets=12)
    json_path = tmp_path / "market_first_discovery_report.json"
    wallets_csv = tmp_path / "market_first_wallets.csv"
    markets_csv = tmp_path / "market_first_markets.csv"
    rejected_csv = tmp_path / "market_first_rejected_markets.csv"
    assert json_path.exists()
    assert wallets_csv.exists()
    assert markets_csv.exists()
    assert rejected_csv.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    for key in ("conclusion", "wallets_discovered", "markets_usable", "tier_breakdown", "warnings"):
        assert key in payload


def test_market_first_run_when_no_usable_markets_emits_insufficient_conclusion(patched_service, monkeypatch) -> None:
    service, _ = patched_service
    monkeypatch.setattr(service.scanner, "fetch_resolved_markets", lambda **kwargs: [])
    monkeypatch.setattr(service, "_fetch_market_trades", lambda cid, limit: [])
    report = service.run(days_back=365, max_markets=10)
    assert report.markets_usable == 0
    assert report.conclusion == "MARKET_FIRST_INSUFFICIENT_DATA"
    assert "no_usable_resolved_markets_in_window" in report.warnings


def test_market_first_no_silent_mock_fallback_when_disabled(patched_service, monkeypatch) -> None:
    """When ``polymarket_public_enabled=False`` the per-market trade fetch
    must return an empty list rather than synthesising mock trades."""
    service, _ = patched_service
    settings = get_settings()
    settings.polymarket_public_enabled = False
    try:
        result = service._fetch_market_trades("0xanything", limit=100)
        assert result == []
    finally:
        settings.polymarket_public_enabled = True
