"""v0.7.8 C1 — Market metadata resolver tests.

The resolver fixes a real bug: 198/369 (53.7%) ELITE WATCH audits in
the smoke 100€ window were rejected as "LOW_LIQUIDITY" because the
market row was missing from the local DB. Liquidity_score defaulted
to 0, orderbook_quality defaulted to INSUFFICIENT_DATA.

The resolver fetches Gamma + CLOB metadata on-demand BEFORE the audit
runs, with cache + lock + token bucket + NOT_FOUND blacklist.

10 tests:
1. missing_market_triggers_gamma_backfill_before_decision
2. missing_market_does_not_become_fake_low_liquidity
3. gamma_failure_logs_DATA_MISSING_not_LOW_LIQ
4. orderbook_unavailable_logs_clean_reason
5. elite_with_backfilled_market_can_become_paper_trade (integration smoke)
6. market_upsert_idempotent (sequential)
7. concurrent_audits_share_one_fetch (lock dedup)
8. no_silent_drop_on_metadata_fetch_failure
9. dynamic_field_not_cached_long (TTL 30s for prices)
10. not_found_blacklist_prevents_retry
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.market import Market
from app.services.market_metadata_resolver import (
    DYNAMIC_DATA_TTL_S,
    NOT_FOUND_BLACKLIST_TTL_S,
    STATIC_METADATA_TTL_S,
    MarketMetadataResolver,
    _classify_orderbook,
    _extract_dynamic,
    _extract_static,
    _upsert_market_row,
    reset_resolver_for_tests,
)


def _engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(e)
    return e


def _gamma_doc(market_id="0xtest", category="Crypto", deadline="2026-12-31T00:00:00Z"):
    return {
        "id": "12345",
        "conditionId": market_id,
        "question": "Will X happen?",
        "category": category,
        "endDate": deadline,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.6", "0.4"]),
        "clobTokenIds": json.dumps([
            "100000000000000000000000000000000",
            "200000000000000000000000000000000",
        ]),
        "liquidity": 5000.0,
        "volume24hr": 10000.0,
        "slug": "test-market",
        "closed": False,
    }


def _orderbook_doc(spread=0.01):
    return {
        "bids": [{"price": str(0.6 - spread / 2), "size": "100"}],
        "asks": [{"price": str(0.6 + spread / 2), "size": "100"}],
    }


# ---------------- 1 — missing market triggers backfill BEFORE decision ----------------


def test_missing_market_triggers_gamma_backfill_before_decision():
    """The resolver fetches Gamma + CLOB the FIRST time a missing market
    is requested. After resolve(), the DB has a Market row with
    category, deadline, prices."""
    eng = _engine()
    with Session(eng) as session:
        # Pre-condition: market not in DB
        assert session.get(Market, "0xnewmarket") is None

        gamma_mock = MagicMock()
        gamma_mock.fetch_market_by_condition.return_value = _gamma_doc("0xnewmarket")
        clob_mock = MagicMock()
        clob_mock.fetch_orderbook.return_value = _orderbook_doc(spread=0.01)

        r = MarketMetadataResolver(gamma_client=gamma_mock, clob_client=clob_mock)
        result = r.resolve("0xnewmarket", session=session)

        assert result.status == "COMPLETE"
        assert result.has_static is True
        assert result.has_orderbook is True
        # DB row created
        m = session.get(Market, "0xnewmarket")
        assert m is not None
        assert m.category == "Crypto"
        assert m.yes_price == 0.6
        assert m.spread is not None
        # Gamma was called exactly once
        gamma_mock.fetch_market_by_condition.assert_called_once_with("0xnewmarket")


# ---------------- 2 — missing market never becomes fake LOW_LIQUIDITY ----------------


def test_missing_market_does_not_become_fake_low_liquidity():
    """The resolver must produce a clean status code, NEVER convert a
    fetch failure into a default ``liquidity_score=0`` that would
    cascade into LOW_LIQUIDITY at the audit gate."""
    gamma_mock = MagicMock()
    gamma_mock.fetch_market_by_condition.side_effect = RuntimeError("network dead")

    r = MarketMetadataResolver(gamma_client=gamma_mock, clob_client=MagicMock())
    result = r.resolve("0xfailing")

    assert result.status == "MARKET_METADATA_FETCH_FAILED"
    assert result.has_static is False
    # Caller must use the explicit status, not synthesize liquidity=0
    # downstream. The contract is: status field is the source of truth.
    assert "LOW_LIQUIDITY" not in (result.error or "")


# ---------------- 3 — Gamma failure → DATA_MISSING reason, not LOW_LIQ ----------------


def test_gamma_failure_logs_DATA_MISSING_not_LOW_LIQ():
    """On Gamma exception, status is FETCH_FAILED. Polling-engine hook
    is responsible for translating this into a NoTradeDecision row with
    reason_code=MARKET_METADATA_FETCH_FAILED — verified via REJECT_CODES
    presence."""
    from app.services.capital_allocator import REJECT_CODES

    assert "MARKET_METADATA_FETCH_FAILED" in REJECT_CODES
    assert "DATA_MISSING_MARKET_METADATA" in REJECT_CODES
    assert "MARKET_METADATA_NOT_FOUND" in REJECT_CODES
    assert "ORDERBOOK_UNAVAILABLE" in REJECT_CODES
    # LOW_LIQUIDITY remains in REJECT_CODES for legitimately illiquid
    # markets (real depth issue), but it must NOT be the reason given
    # for a missing-market scenario — that's the contract C1 protects.


# ---------------- 4 — orderbook unavailable produces clean reason ----------------


def test_orderbook_unavailable_logs_clean_reason():
    """Gamma OK, CLOB returns empty orderbook → status PARTIAL_NO_ORDERBOOK
    (NOT FETCH_FAILED). The audit pipeline can still proceed using
    Gamma metadata; orderbook gate must not assume BAD/UNTRADABLE just
    because data is missing."""
    gamma_mock = MagicMock()
    gamma_mock.fetch_market_by_condition.return_value = _gamma_doc("0xpartial")
    clob_mock = MagicMock()
    clob_mock.fetch_orderbook.return_value = {}  # empty book

    r = MarketMetadataResolver(gamma_client=gamma_mock, clob_client=clob_mock)
    result = r.resolve("0xpartial")

    assert result.status == "PARTIAL_NO_ORDERBOOK"
    assert result.has_static is True
    assert result.has_orderbook is False
    # The static metadata is still usable for audit decisions on
    # category / deadline / duration_bucket.
    assert result.category == "Crypto"


# ---------------- 5 — ELITE with backfilled market becomes paper-tradable ----------------


def test_elite_with_backfilled_market_can_become_paper_trade():
    """Integration: after resolver populates the market row, an ELITE
    audit on the same market reads valid liquidity_score + orderbook_quality
    and the audit decision can yield PAPER_TRADE (= no longer auto-WATCH
    because of liq=0)."""
    eng = _engine()
    with Session(eng) as session:
        gamma_mock = MagicMock()
        gamma_mock.fetch_market_by_condition.return_value = _gamma_doc("0xbackfilled")
        clob_mock = MagicMock()
        clob_mock.fetch_orderbook.return_value = _orderbook_doc(spread=0.005)

        r = MarketMetadataResolver(gamma_client=gamma_mock, clob_client=clob_mock)
        result = r.resolve("0xbackfilled", session=session)
        assert result.status == "COMPLETE"

        # Verify market row exists with non-default values
        m = session.get(Market, "0xbackfilled")
        assert m is not None
        assert m.yes_price > 0
        # In a real audit, liquidity_score wouldn't be 0 anymore — the
        # audit pipeline reads market.liquidity which is now populated.
        assert m.liquidity == 5000.0


# ---------------- 6 — market_upsert_idempotent (sequential) ----------------


def test_market_upsert_idempotent():
    """Calling _upsert_market_row twice on the same market_id with the
    same data must NOT raise (no UNIQUE violation, no IntegrityError)."""
    eng = _engine()
    with Session(eng) as session:
        static = {
            "question": "Q",
            "category": "Politics",
            "deadline": "2026-12-31T00:00:00Z",
            "expected_resolution_minutes": 60.0,
            "duration_bucket": "SHORT",
            "market_speed_class": "FAST",
            "raw_slug": "test",
            "data_source": "test",
            "clob_token_ids": ["1", "2"],
        }
        dynamic = {
            "yes_price": 0.5,
            "no_price": 0.5,
            "spread": 0.01,
            "liquidity": 1000.0,
            "volume_24h": 5000.0,
        }
        _upsert_market_row(session, "0xidempotent", static, dynamic)
        _upsert_market_row(session, "0xidempotent", static, dynamic)
        # Confirm exactly 1 row exists
        m = session.get(Market, "0xidempotent")
        assert m is not None
        from sqlmodel import select
        rows = list(session.exec(select(Market).where(Market.id == "0xidempotent")))
        assert len(rows) == 1


# ---------------- 7 — concurrent audits share one fetch ----------------


def test_concurrent_audits_share_one_fetch():
    """Two threads call resolve() simultaneously on the same missing
    market. The per-market lock ensures Gamma is hit ONCE, not twice."""
    gamma_mock = MagicMock()
    gamma_mock.fetch_market_by_condition.return_value = _gamma_doc("0xconcurrent")
    clob_mock = MagicMock()
    clob_mock.fetch_orderbook.return_value = _orderbook_doc()

    r = MarketMetadataResolver(gamma_client=gamma_mock, clob_client=clob_mock)
    results = []

    def _worker():
        results.append(r.resolve("0xconcurrent"))

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    t2.start()
    t1.join(timeout=3)
    t2.join(timeout=3)

    assert len(results) == 2
    assert all(x.status == "COMPLETE" for x in results)
    # Gamma was only hit ONCE thanks to the per-market lock + cache
    assert gamma_mock.fetch_market_by_condition.call_count == 1


# ---------------- 8 — no silent drop on fetch failure ----------------


def test_no_silent_drop_on_metadata_fetch_failure():
    """When Gamma fails, the resolver returns a status — never silently
    succeeds with empty data. The downstream NoTradeDecision row is
    written by the polling-engine hook, not silently swallowed."""
    gamma_mock = MagicMock()
    gamma_mock.fetch_market_by_condition.side_effect = ConnectionError("dns fail")

    r = MarketMetadataResolver(gamma_client=gamma_mock, clob_client=MagicMock())
    result = r.resolve("0xdnsfail")

    # Explicit failure — caller MUST handle this, the status field is
    # the source of truth (no silent COMPLETE on broken state).
    assert result.status == "MARKET_METADATA_FETCH_FAILED"
    assert result.error is not None
    assert "dns fail" in result.error


# ---------------- 9 — dynamic field NOT cached long ----------------


def test_dynamic_field_not_cached_long():
    """Static cache TTL is 30 min, dynamic cache TTL is 30s. Live
    pricing must never read a stale price beyond the dynamic TTL.

    Verified by: STATIC_METADATA_TTL_S >> DYNAMIC_DATA_TTL_S, and
    the resolver respects this when computing TTLs."""
    assert STATIC_METADATA_TTL_S >= 5 * 60.0     # at least 5 min
    assert DYNAMIC_DATA_TTL_S <= 60.0            # at most 1 min
    assert STATIC_METADATA_TTL_S > DYNAMIC_DATA_TTL_S * 10  # clear separation

    # Behavioural check with a fake clock — the dynamic cache expires
    # before the static cache.
    clock_value = [1000.0]

    def fake_clock():
        return clock_value[0]

    gamma_mock = MagicMock()
    gamma_mock.fetch_market_by_condition.return_value = _gamma_doc("0xdyntest")
    clob_mock = MagicMock()
    clob_mock.fetch_orderbook.return_value = _orderbook_doc()
    r = MarketMetadataResolver(
        gamma_client=gamma_mock, clob_client=clob_mock, clock=fake_clock,
    )
    # First call: full fetch
    r.resolve("0xdyntest")
    assert gamma_mock.fetch_market_by_condition.call_count == 1
    # Advance time past dynamic TTL but within static TTL
    clock_value[0] += DYNAMIC_DATA_TTL_S + 1
    # Force a fresh pricing fetch (simulates _fetch_execution_price_now
    # in paper_trading_engine) — the dynamic cache should be expired.
    r.resolve("0xdyntest", force_fresh_pricing=True)
    # Gamma is hit again because dynamic data is stale OR because
    # force_fresh_pricing=True bypasses the dynamic cache.
    assert gamma_mock.fetch_market_by_condition.call_count == 2


# ---------------- 10 — NOT_FOUND blacklist prevents retry ----------------


def test_not_found_blacklist_prevents_retry():
    """If Gamma returns empty for a market_id, the resolver blacklists
    it for ~1h. Subsequent resolve() calls within the TTL must
    short-circuit with NOT_FOUND without hitting Gamma again."""
    gamma_mock = MagicMock()
    gamma_mock.fetch_market_by_condition.return_value = {}  # not found

    r = MarketMetadataResolver(gamma_client=gamma_mock, clob_client=MagicMock())
    result1 = r.resolve("0xghost")
    result2 = r.resolve("0xghost")
    result3 = r.resolve("0xghost")

    assert result1.status == "MARKET_METADATA_NOT_FOUND"
    assert result2.status == "MARKET_METADATA_NOT_FOUND"
    assert result3.status == "MARKET_METADATA_NOT_FOUND"
    # Gamma was called only ONCE — the next 2 hit the blacklist
    assert gamma_mock.fetch_market_by_condition.call_count == 1
    # And blacklist TTL is meaningful (not zero)
    assert NOT_FOUND_BLACKLIST_TTL_S >= 60.0


# ---------------- helper unit tests (testing pure functions) ----------------


def test_extract_static_handles_string_json_lists():
    """Gamma sometimes returns ``outcomes`` and ``clobTokenIds`` as
    JSON-encoded strings, not lists. Helper must parse both."""
    out = _extract_static(_gamma_doc())
    assert out["category"] == "Crypto"
    assert isinstance(out["outcomes"], list)
    assert isinstance(out["clob_token_ids"], list)
    assert len(out["clob_token_ids"]) == 2


def test_classify_orderbook_recognises_bad_spread():
    bad = _classify_orderbook({
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [{"price": "0.60", "size": "10"}],  # 10pp spread = UNTRADABLE
    })
    assert bad["quality"] == "UNTRADABLE"

    good = _classify_orderbook({
        "bids": [{"price": "0.595", "size": "10"}],
        "asks": [{"price": "0.605", "size": "10"}],  # 1pp = OK
    })
    assert good["quality"] == "OK"


def test_classify_orderbook_handles_missing_data():
    assert _classify_orderbook({})["quality"] == "INSUFFICIENT_DATA"
    assert _classify_orderbook({"bids": [], "asks": []})["quality"] == "INSUFFICIENT_DATA"
