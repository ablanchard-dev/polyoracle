"""v0.7.6 B11 — category ingestion + slug fallback tests."""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from app.models.market import Market
from app.services.category_inference import (
    infer_category,
    backfill_categories_in_db,
)


def test_category_from_gamma_metadata():
    """When gamma_category is set, return it normalized — slug/question
    are ignored."""
    assert infer_category(gamma_category="Politics", slug="btc-up", question="?") == "Politics"
    assert infer_category(gamma_category="crypto", slug="?", question="?") == "Crypto"
    assert infer_category(gamma_category="Geopolitical", slug="?", question="?") == "Geopolitics"
    assert infer_category(gamma_category="macro", slug="?", question="?") == "Economics"


def test_category_fallback_slug_parsing():
    """When gamma_category is missing, infer from the slug keywords."""
    assert infer_category(slug="btc-up-or-down-2026-05-03") == "Crypto"
    assert infer_category(slug="trump-2024-presidential-election") in {"Geopolitics", "Politics"}
    assert infer_category(slug="superbowl-2026-winner") == "Sports"
    assert infer_category(slug="fed-rate-hike-march-2026") == "Economics"
    assert infer_category(slug="russia-ukraine-ceasefire-2026") == "Geopolitics"
    assert infer_category(slug="best-picture-oscar-2026") == "Culture"


def test_category_unknown_when_no_data():
    """When neither category nor slug nor question give a clue, return
    'Unknown' — explicit, not silent."""
    assert infer_category() == "Unknown"
    assert infer_category(slug="foo-bar-baz", question="random text") == "Unknown"
    assert infer_category(gamma_category="", slug="", question="") == "Unknown"


def test_category_question_fallback_when_slug_silent():
    """If slug is uninformative but the question carries category clues,
    use the question."""
    assert infer_category(
        slug="market-12345",
        question="Will Bitcoin close above $50k on Friday?",
    ) == "Crypto"
    assert infer_category(
        slug="poll-x",
        question="Will the Fed cut rates at the May FOMC meeting?",
    ) == "Economics"


def test_backfill_categories_in_db():
    """The DB-walk variant updates rows with NULL category, leaves others
    intact, and returns a distribution summary."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    sess = Session(engine)

    sess.add(Market(id="m1", question="Will BTC close above $70k?", raw_slug="btc-70k", category=None))
    sess.add(Market(id="m2", question="random", raw_slug="random-2026", category=None))
    sess.add(Market(id="m3", question="?", raw_slug="?", category="Politics"))  # already set
    sess.commit()

    result = backfill_categories_in_db(sess)
    assert result["scanned"] == 2  # only NULL rows scanned
    # m1 should pick up Crypto from slug
    m1 = sess.get(Market, "m1")
    assert m1.category == "Crypto"
    # m2 stays NULL (no keyword) — but is reported in distribution as Unknown
    m2 = sess.get(Market, "m2")
    assert m2.category in (None, "")
    assert "Unknown" in result["distribution_after_inference"]
    # m3 (already Politics) untouched
    m3 = sess.get(Market, "m3")
    assert m3.category == "Politics"
