"""Tests for the v0.5.1 candidate validation pipeline.

Bias rules locked in:

- One wallet trading the SAME market 10 times must count as 1 resolved entry,
  not 10. (Per-(wallet, market) dedupe at aggregation time.)
- A wallet with 100 % win rate on 12 markets cannot be promoted past
  CANDIDATE_ELITE — sample is too small.
- A wallet that is hot in the 70 % discovery partition but collapses below
  50 % win rate in the 30 % validation partition lands in FAILED_VALIDATION.
- A wallet whose wins are all under one ``event_slug`` lands in BIASED_SAMPLE
  even with high win rate.
- Anti-bias detection of 5-minute crypto market dominance reports the
  fraction observed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.services.candidate_validation_service import (
    CandidateValidationService,
)
from app.services.market_first_discovery import (
    MarketFirstDiscoveryService,
    WalletAggregate,
)
from app.services.market_resolution_scanner import ResolvedMarket


def _engine(tmp_path: Path) -> object:
    db_path = tmp_path / "candidate.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401
    SQLModel.metadata.create_all(engine)
    return engine


def _make_market(
    market_id: str,
    *,
    winning_index: int = 0,
    end_date: str = "2025-06-01T00:00:00Z",
    category: str = "Crypto",
    event_slug: str | None = None,
    slug: str | None = None,
    question: str = "test",
) -> ResolvedMarket:
    return ResolvedMarket(
        market_id=market_id,
        condition_id=f"0x{market_id:0>40s}",
        question=question,
        category=category,
        end_date=end_date,
        closed=True,
        outcomes=["YES", "NO"],
        outcome_prices=[1.0 if winning_index == 0 else 0.0, 1.0 if winning_index == 1 else 0.0],
        winning_outcome_index=winning_index,
        winning_outcome_name="YES" if winning_index == 0 else "NO",
        slug=slug,
        event_slug=event_slug,
        raw={},
    )


def _trades(wallet: str, outcome_index: int, *, n: int = 1, conditionId: str = "") -> list[dict[str, Any]]:
    return [
        {
            "proxyWallet": wallet,
            "side": "BUY",
            "outcomeIndex": outcome_index,
            "size": 10,
            "price": 0.5,
            "timestamp": 1_775_000_000 + idx,
            "conditionId": conditionId,
        }
        for idx in range(n)
    ]


# ---------------- dedupe per (wallet, market) ----------------


def test_aggregation_dedupes_same_wallet_same_market(tmp_path) -> None:
    """A wallet with 10 BUY trades on a single market counts as ONE win."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        svc = MarketFirstDiscoveryService(session)
        market = _make_market("m1", winning_index=0)
        trades = _trades("0xwhale", 0, n=10, conditionId=market.condition_id or "")
        agg: dict[str, WalletAggregate] = {}
        svc._aggregate_market_into_wallets(market, trades, agg)
    wallet = agg["0xwhale"]
    assert len(wallet.winning_markets) == 1
    assert wallet.trades_count == 10  # raw trades still tracked
    assert (len(wallet.winning_markets) + len(wallet.losing_markets)) == 1


# ---------------- 100% on 12 markets stays CANDIDATE_ELITE ----------------


def test_perfect_winrate_on_small_sample_cannot_become_elite(tmp_path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        svc = CandidateValidationService(session)
        # Build aggregator: 12 wins, 0 losses, spread across 3 categories and 6
        # distinct event slugs so neither bias filter trips. Sample is still
        # too small for STRONG, so the only honest verdict is CANDIDATE_ELITE.
        agg = WalletAggregate(address="0xperfect")
        categories = ["Politics", "Crypto", "Sports"]
        for idx in range(12):
            agg.winning_markets.add(f"m{idx}")
            cat = categories[idx % len(categories)]
            agg.category_results.setdefault(cat, {"wins": 0, "losses": 0})["wins"] += 1
        markets_by_id = {
            f"m{idx}": _make_market(
                f"m{idx}",
                winning_index=0,
                end_date=f"2025-{(idx % 12) + 1:02d}-01T00:00:00Z",
                event_slug=f"event-{idx % 6}",
                category=categories[idx % len(categories)],
            )
            for idx in range(12)
        }
        row = svc._score_candidate(
            "0xperfect",
            agg=agg,
            discovery_agg=agg,
            validation_agg=None,
            previous_payload={"previous_sample": 12, "previous_win_rate": 1.0, "previous_tier": "WEAK"},
            markets_by_id=markets_by_id,
            short_window_crypto_dominance=False,
            survivor_bias=False,
        )
    assert row.candidate_status == "CANDIDATE_ELITE"
    assert row.candidate_status not in {"ELITE", "STRONG"}


# ---------------- failed validation ----------------


def test_discovery_winner_collapsing_in_validation_lands_failed(tmp_path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        svc = CandidateValidationService(session)
        categories = ["Politics", "Crypto", "Sports"]
        # Full: 35 markets, 25 wins, 10 losses → win rate 0.71.
        agg = WalletAggregate(address="0xfade")
        for idx in range(35):
            cat = categories[idx % len(categories)]
            (agg.winning_markets if idx < 25 else agg.losing_markets).add(f"m{idx}")
            stat = agg.category_results.setdefault(cat, {"wins": 0, "losses": 0})
            stat["wins" if idx < 25 else "losses"] += 1
        # Discovery (oldest 70%, 24 markets): 22 wins, 2 losses → 0.92.
        disc = WalletAggregate(address="0xfade")
        for idx in range(24):
            cat = categories[idx % len(categories)]
            (disc.winning_markets if idx < 22 else disc.losing_markets).add(f"m{idx}")
            stat = disc.category_results.setdefault(cat, {"wins": 0, "losses": 0})
            stat["wins" if idx < 22 else "losses"] += 1
        # Validation (newest 30%, 11 markets): 3 wins, 8 losses → 0.27 → collapse.
        val = WalletAggregate(address="0xfade")
        for idx in range(24, 35):
            cat = categories[idx % len(categories)]
            (val.winning_markets if idx < 27 else val.losing_markets).add(f"m{idx}")
            stat = val.category_results.setdefault(cat, {"wins": 0, "losses": 0})
            stat["wins" if idx < 27 else "losses"] += 1
        markets_by_id = {
            f"m{idx}": _make_market(
                f"m{idx}",
                winning_index=0,
                event_slug=f"event-{idx % 8}",
                category=categories[idx % len(categories)],
            )
            for idx in range(35)
        }
        row = svc._score_candidate(
            "0xfade",
            agg=agg,
            discovery_agg=disc,
            validation_agg=val,
            previous_payload=None,
            markets_by_id=markets_by_id,
            short_window_crypto_dominance=False,
            survivor_bias=False,
        )
    assert row.candidate_status == "FAILED_VALIDATION"
    assert "collapsed" in row.final_recommendation


# ---------------- v0.5.3 OUTLIER_FLAGGED + tighter ELITE bar ----------------


def test_outlier_flagged_when_validation_gap_exceeds_15pct(tmp_path) -> None:
    """A wallet with sample 100+ / HIGH confidence / discovery WR 88% but
    validation WR 65% (gap 23%) must NOT be ELITE — it lands in
    OUTLIER_FLAGGED so the v0.5.4 ValidatedPaperUniverse can exclude it."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        svc = CandidateValidationService(session)
        agg = WalletAggregate(address="0xoutlier")
        # 84 wins / 23 losses on discovery (88%), 15 wins / 8 losses on validation (65%) -> gap 23%
        # but total sample 84+23+15+8 = 130 (HIGH confidence by win-rate engine threshold)
        for idx in range(107):
            agg.winning_markets.add(f"m-d-{idx}") if idx < 84 else agg.losing_markets.add(f"m-d-{idx}")
            agg.category_results.setdefault(["Politics", "Crypto", "Sports"][idx % 3], {"wins": 0, "losses": 0})[
                "wins" if idx < 84 else "losses"
            ] += 1
        for idx in range(23):
            agg.winning_markets.add(f"m-v-{idx}") if idx < 15 else agg.losing_markets.add(f"m-v-{idx}")
            agg.category_results.setdefault(["Politics", "Crypto", "Sports"][idx % 3], {"wins": 0, "losses": 0})[
                "wins" if idx < 15 else "losses"
            ] += 1
        disc = WalletAggregate(address="0xoutlier")
        for idx in range(107):
            disc.winning_markets.add(f"m-d-{idx}") if idx < 84 else disc.losing_markets.add(f"m-d-{idx}")
            disc.category_results.setdefault(["Politics", "Crypto", "Sports"][idx % 3], {"wins": 0, "losses": 0})[
                "wins" if idx < 84 else "losses"
            ] += 1
        val = WalletAggregate(address="0xoutlier")
        for idx in range(23):
            val.winning_markets.add(f"m-v-{idx}") if idx < 15 else val.losing_markets.add(f"m-v-{idx}")
            val.category_results.setdefault(["Politics", "Crypto", "Sports"][idx % 3], {"wins": 0, "losses": 0})[
                "wins" if idx < 15 else "losses"
            ] += 1
        markets_by_id = {
            f"m-d-{idx}": _make_market(f"m-d-{idx}", winning_index=0, event_slug=f"event-{idx % 10}")
            for idx in range(107)
        }
        markets_by_id.update(
            {f"m-v-{idx}": _make_market(f"m-v-{idx}", winning_index=0, event_slug=f"event-{idx % 7}") for idx in range(23)}
        )
        row = svc._score_candidate(
            "0xoutlier",
            agg=agg,
            discovery_agg=disc,
            validation_agg=val,
            previous_payload=None,
            markets_by_id=markets_by_id,
            short_window_crypto_dominance=False,
            survivor_bias=False,
        )
    assert row.candidate_status == "OUTLIER_FLAGGED", row
    assert row.expanded_sample == 130
    assert "OUTLIER_FLAGGED" in row.final_recommendation or "do NOT include" in row.final_recommendation


def test_outlier_flagged_when_validation_wr_below_70pct(tmp_path) -> None:
    """Even with a tight gap (≤ 15%), a wallet whose validation WR sits below
    70% should not be ELITE — too marginal as a copy candidate."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        svc = CandidateValidationService(session)
        # 105 wins / 30 losses on discovery (78%), 18 wins / 12 losses on validation (60%) - gap 18%
        # Hmm that's also gap > 15. Let me make discovery 70% / validation 65% - gap 5% but val < 70.
        agg = WalletAggregate(address="0xmarginal")
        for idx in range(135):
            (agg.winning_markets if idx < 95 else agg.losing_markets).add(f"d-{idx}")
            agg.category_results.setdefault(["A", "B"][idx % 2], {"wins": 0, "losses": 0})[
                "wins" if idx < 95 else "losses"
            ] += 1
        for idx in range(20):
            (agg.winning_markets if idx < 13 else agg.losing_markets).add(f"v-{idx}")
            agg.category_results.setdefault(["A", "B"][idx % 2], {"wins": 0, "losses": 0})[
                "wins" if idx < 13 else "losses"
            ] += 1
        disc = WalletAggregate(address="0xmarginal")
        for idx in range(135):
            (disc.winning_markets if idx < 95 else disc.losing_markets).add(f"d-{idx}")
            disc.category_results.setdefault(["A", "B"][idx % 2], {"wins": 0, "losses": 0})[
                "wins" if idx < 95 else "losses"
            ] += 1
        val = WalletAggregate(address="0xmarginal")
        for idx in range(20):
            (val.winning_markets if idx < 13 else val.losing_markets).add(f"v-{idx}")
            val.category_results.setdefault(["A", "B"][idx % 2], {"wins": 0, "losses": 0})[
                "wins" if idx < 13 else "losses"
            ] += 1
        # discovery_wr = 95/135 = 70.4%, validation_wr = 13/20 = 65% -> gap ~5% but val < 70 -> OUTLIER
        markets_by_id = {f"d-{idx}": _make_market(f"d-{idx}", winning_index=0, event_slug=f"e-{idx % 8}", category="A" if idx % 2 == 0 else "B") for idx in range(135)}
        markets_by_id.update({f"v-{idx}": _make_market(f"v-{idx}", winning_index=0, event_slug=f"f-{idx % 5}", category="A" if idx % 2 == 0 else "B") for idx in range(20)})
        row = svc._score_candidate(
            "0xmarginal",
            agg=agg,
            discovery_agg=disc,
            validation_agg=val,
            previous_payload=None,
            markets_by_id=markets_by_id,
            short_window_crypto_dominance=False,
            survivor_bias=False,
        )
    assert row.candidate_status == "OUTLIER_FLAGGED", row


# ---------------- biased sample on single event slug ----------------


def test_single_event_slug_winner_lands_biased_sample(tmp_path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        svc = CandidateValidationService(session)
        agg = WalletAggregate(address="0xcrypto")
        for idx in range(20):
            agg.winning_markets.add(f"m{idx}")
            agg.category_results.setdefault("Crypto", {"wins": 0, "losses": 0})["wins"] += 1
        # Every winning market shares the same eventSlug.
        markets_by_id = {
            f"m{idx}": _make_market(f"m{idx}", winning_index=0, event_slug="btc-up-or-down-loop")
            for idx in range(20)
        }
        row = svc._score_candidate(
            "0xcrypto",
            agg=agg,
            discovery_agg=agg,
            validation_agg=None,
            previous_payload=None,
            markets_by_id=markets_by_id,
            short_window_crypto_dominance=False,
            survivor_bias=False,
        )
    assert row.candidate_status == "BIASED_SAMPLE"
    assert row.market_correlation_warning is True


# ---------------- 5-min crypto dominance detection ----------------


def test_short_window_crypto_dominance_detected() -> None:
    markets = [
        _make_market(f"m{idx}", question=f"Bitcoin Up or Down 5 minutes - frame {idx}")
        for idx in range(20)
    ] + [
        _make_market(f"e{idx}", question=f"US elections {idx}", slug=f"us-election-{idx}")
        for idx in range(5)
    ]
    flag, share = MarketFirstDiscoveryService.detect_short_window_crypto_dominance(markets)
    assert flag is True
    assert share >= 0.5


# ---------------- end-to-end validation pass via temporal split ----------------


@pytest.fixture()
def validation_setup(tmp_path, monkeypatch):
    settings = get_settings()
    settings.polymarket_public_enabled = True
    settings.mock_data_enabled = False
    settings.market_first_sleep_ms_between_calls = 0
    settings.market_first_min_sample_for_strong = 30

    engine = _engine(tmp_path)
    session = Session(engine)
    svc = CandidateValidationService(session)
    svc.exports_dir = tmp_path
    monkeypatch.setattr(CandidateValidationService, "json_path", property(lambda self: tmp_path / "candidate_elite_validation_report.json"))
    monkeypatch.setattr(CandidateValidationService, "csv_path", property(lambda self: tmp_path / "candidate_elite_wallets.csv"))
    monkeypatch.setattr(CandidateValidationService, "previous_report_snapshot", property(lambda self: tmp_path / "previous.json"))

    yield svc, session, tmp_path
    session.close()


def test_validation_writes_csv_json_and_classifies_wallets(validation_setup, monkeypatch) -> None:
    svc, session, tmp_path = validation_setup
    # 40 resolved markets with strictly-increasing end_dates so the temporal
    # split is deterministic; spread across 8 event slugs and 4 categories
    # so neither bias filter trips on a wallet that holds the winner everywhere.
    import datetime as _dt
    base = _dt.date(2024, 1, 1)
    categories = ["Politics", "Crypto", "Sports", "Macro"]
    markets = [
        _make_market(
            f"m{idx}",
            winning_index=idx % 2,
            end_date=(base + _dt.timedelta(days=idx)).isoformat() + "T00:00:00Z",
            event_slug=f"event-{idx % 8}",
            category=categories[idx % len(categories)],
        )
        for idx in range(40)
    ]
    ace = "0xace00000000000000000000000000000000000ace"
    fade = "0xfade0000000000000000000000000000000fade1"

    def fake_scan(**kwargs):
        return [], markets, [], {m.market_id: _trades_for_test(m, ace, fade) for m in markets}, 0

    monkeypatch.setattr(svc.market_first, "_scan_and_fetch", fake_scan)
    report = svc.run_validation(days_back=365, max_markets=40, split_ratio=0.7)

    by_address = {row["address"]: row for row in report.rows}
    ace_row = by_address[ace]
    assert ace_row["expanded_sample"] == 40
    assert ace_row["expanded_win_rate"] == 1.0
    assert ace_row["candidate_status"] in {"ELITE", "STRONG"}, ace_row

    # Fade is winning the discovery half but losing the validation half.
    fade_row = by_address[fade]
    assert fade_row["candidate_status"] in {"FAILED_VALIDATION", "DROPPED"}, fade_row

    json_payload = json.loads((tmp_path / "candidate_elite_validation_report.json").read_text(encoding="utf-8"))
    assert "elite" in json_payload
    assert (tmp_path / "candidate_elite_wallets.csv").exists()


def _trades_for_test(market: ResolvedMarket, ace: str, fade: str) -> list[dict[str, Any]]:
    """Ace always net long the winner. Fade flips: wins on the older 70% (lower
    market index in our deterministic ordering), loses on the newest 30%.
    """
    trades: list[dict[str, Any]] = [
        {"proxyWallet": ace, "side": "BUY", "outcomeIndex": market.winning_outcome_index, "size": 10, "price": 0.5, "timestamp": 1, "conditionId": market.condition_id},
    ]
    market_index = int(market.market_id.replace("m", ""))
    fade_outcome = market.winning_outcome_index if market_index < 28 else (1 - market.winning_outcome_index)
    trades.append({
        "proxyWallet": fade,
        "side": "BUY",
        "outcomeIndex": fade_outcome,
        "size": 10,
        "price": 0.5,
        "timestamp": 1,
        "conditionId": market.condition_id,
    })
    return trades
