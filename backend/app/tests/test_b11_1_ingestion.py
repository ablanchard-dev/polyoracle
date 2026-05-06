"""v0.7.7 B11.1 — categories at ingestion tests."""

from __future__ import annotations

from app.services.polymarket.normalizers import normalize_gamma_market


def test_normalize_gamma_market_uses_metadata_when_present():
    """Gamma-supplied category survives normalization."""
    payload = {
        "id": "0xm1",
        "question": "?",
        "category": "Politics",
        "slug": "btc-up",  # would otherwise infer Crypto
    }
    m = normalize_gamma_market(payload)
    assert m.category == "Politics"
    assert m.data_source == "polymarket_gamma+metadata"


def test_normalize_gamma_market_infers_category_from_slug():
    """No category in payload → inferred from slug keyword."""
    payload = {
        "id": "0xm2",
        "question": "?",
        "slug": "btc-close-above-70k-2026-05-03",
    }
    m = normalize_gamma_market(payload)
    assert m.category == "Crypto"
    assert m.data_source == "polymarket_gamma+inferred"


def test_normalize_gamma_market_infers_from_question_when_slug_silent():
    payload = {
        "id": "0xm3",
        "question": "Will the Fed cut rates at the May FOMC meeting?",
        "slug": "market-12345",
    }
    m = normalize_gamma_market(payload)
    assert m.category == "Economics"
    assert m.data_source == "polymarket_gamma+inferred"


def test_unknown_only_when_no_data_available():
    """When neither metadata nor slug nor question give clues, "Unknown"
    is set explicitly (not NULL) and source flag is 'unknown'."""
    payload = {"id": "0xm4", "question": "random", "slug": "random-2026"}
    m = normalize_gamma_market(payload)
    assert m.category == "Unknown"
    assert m.data_source == "polymarket_gamma+unknown"


def test_category_source_tracked():
    """data_source ends with one of '+metadata' / '+inferred' / '+unknown'
    so observability can split the categorization origin."""
    p_meta = {"id": "x", "question": "?", "category": "Sports"}
    p_infer = {"id": "y", "question": "?", "slug": "nfl-superbowl"}
    p_unknown = {"id": "z", "question": "?", "slug": "abc"}
    assert normalize_gamma_market(p_meta).data_source.endswith("+metadata")
    assert normalize_gamma_market(p_infer).data_source.endswith("+inferred")
    assert normalize_gamma_market(p_unknown).data_source.endswith("+unknown")
