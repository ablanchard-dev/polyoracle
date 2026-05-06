from app.services.polymarket.normalizers import normalize_gamma_market, parse_jsonish_list, safe_float


def test_parse_jsonish_list_handles_gamma_strings() -> None:
    assert parse_jsonish_list('["Yes", "No"]') == ["Yes", "No"]
    assert parse_jsonish_list("") == []


def test_safe_float_handles_empty_values() -> None:
    assert safe_float("12.5") == 12.5
    assert safe_float(None) == 0.0


def test_normalize_gamma_market_maps_prices_and_tokens() -> None:
    market = normalize_gamma_market(
        {
            "id": "123",
            "question": "Will it happen?",
            "category": "Test",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.41", "0.59"]',
            "clobTokenIds": '["token-yes", "token-no"]',
            "volume24hr": "1000",
            "volume1wk": "5000",
            "liquidity": "2500",
            "endDate": "2026-12-31T00:00:00Z",
            "active": True,
        }
    )

    assert market.id == "123"
    assert market.yes_price == 0.41
    assert market.no_price == 0.59
    assert market.clob_token_ids == '["token-yes", "token-no"]'
    # v0.7.7 B11.1 — data_source now carries category source flag
    assert market.data_source.startswith("polymarket_gamma")
