"""Tests for the category resolver fallback chain (Phase A B-fix — 2026-05-11)."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.market import Market
from app.models.wallet import ResolvedMarketRecord
from app.services.category_resolver import (
    _UNKNOWN_LABELS,
    _is_unknown,
    resolve_category_with_fallback,
)


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def test_is_unknown_recognizes_empty_and_aliases():
    assert _is_unknown(None) is True
    assert _is_unknown("") is True
    assert _is_unknown("unknown") is True
    assert _is_unknown("Unknown") is True
    assert _is_unknown("uncategorised") is True
    assert _is_unknown("UNCATEGORIZED") is True
    assert _is_unknown("Crypto") is False
    assert _is_unknown("Politics") is False


def test_hint_short_circuits_when_canonical(session):
    """Caller's hint is used directly when not unknown."""
    cat, source = resolve_category_with_fallback(session, "0xmkt", hint_category="Crypto")
    assert cat == "Crypto"
    assert source == "hint"


def test_market_db_used_when_hint_unknown(session):
    session.add(Market(id="0xmkt-1", question="Test", category="Sports"))
    session.commit()
    cat, source = resolve_category_with_fallback(session, "0xmkt-1", hint_category="Unknown")
    assert cat == "Sports"
    assert source == "market_db"


def test_resolved_market_record_fallback(session):
    """Market has NULL category but ResolvedMarketRecord has it."""
    session.add(Market(id="0xmkt-2", question="Test", category=None))
    session.add(ResolvedMarketRecord(
        market_id="0xmkt-2", condition_id="0xmkt-2", question="Test",
        category="Politics", end_date="2024-06-15", closed=True,
        winning_outcome_index=0, winning_outcome_name="Yes", usable=True,
    ))
    session.commit()
    cat, source = resolve_category_with_fallback(session, "0xmkt-2")
    assert cat == "Politics"
    assert source == "resolved_market_record"


def test_inference_from_question_keywords(session):
    """No Market.category, no ResolvedMarketRecord — fallback to keyword inference."""
    session.add(Market(
        id="0xmkt-3", question="Will Bitcoin hit $100k by year-end?", category=None,
        raw_slug="will-bitcoin-hit-100k",
    ))
    session.commit()
    cat, source = resolve_category_with_fallback(session, "0xmkt-3")
    assert cat == "Crypto"
    assert source == "inferred"


def test_inference_from_slug_only(session):
    session.add(Market(
        id="0xmkt-4", question="Some market", category=None,
        raw_slug="ethereum-merge-completes",
    ))
    session.commit()
    cat, source = resolve_category_with_fallback(session, "0xmkt-4")
    assert cat == "Crypto"
    assert source == "inferred"


def test_truly_unknown_returns_unknown(session):
    """When nothing infers, return 'Unknown' so the safety gate still triggers."""
    session.add(Market(
        id="0xmkt-5", question="Random unparseable text xyz123", category=None,
        raw_slug="xyz-random",
    ))
    session.commit()
    cat, source = resolve_category_with_fallback(session, "0xmkt-5")
    assert cat == "Unknown"
    assert source == "unknown"


def test_no_market_id_returns_unknown(session):
    cat, source = resolve_category_with_fallback(session, None)
    assert cat == "Unknown"
    assert source == "unknown"


def test_market_uncategorised_falls_through_to_resolved(session):
    """Market.category='Uncategorised' triggers fallback even though non-null."""
    session.add(Market(id="0xmkt-6", question="Test", category="Uncategorised"))
    session.add(ResolvedMarketRecord(
        market_id="0xmkt-6", condition_id="0xmkt-6", question="Test",
        category="Crypto", end_date="2024-06-15", closed=True,
        winning_outcome_index=0, winning_outcome_name="Yes", usable=True,
    ))
    session.commit()
    cat, source = resolve_category_with_fallback(session, "0xmkt-6")
    assert cat == "Crypto"
    assert source == "resolved_market_record"


def test_hint_uncategorised_falls_through(session):
    """Hint='Uncategorised' triggers fallback even though non-null."""
    session.add(Market(id="0xmkt-7", question="Test", category="Politics"))
    session.commit()
    cat, source = resolve_category_with_fallback(
        session, "0xmkt-7", hint_category="Uncategorised"
    )
    assert cat == "Politics"
    assert source == "market_db"
