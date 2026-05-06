from app.services.orderbook_analyzer import OrderbookAnalyzer


SAMPLE = {
    "token_id": "token-yes",
    "orderbook": {
        "bids": [
            {"price": 0.498, "size": 60_000},
            {"price": 0.495, "size": 40_000},
            {"price": 0.49, "size": 20_000},
        ],
        "asks": [
            {"price": 0.502, "size": 55_000},
            {"price": 0.505, "size": 35_000},
            {"price": 0.51, "size": 15_000},
        ],
    },
    "midpoint": {"mid": 0.50},
}


def test_orderbook_basic_metrics() -> None:
    analyzer = OrderbookAnalyzer(SAMPLE)
    assert analyzer.compute_best_bid() == 0.498
    assert analyzer.compute_best_ask() == 0.502
    assert analyzer.compute_midpoint() == 0.50
    assert round(analyzer.compute_spread() or 0, 6) == 0.004
    assert analyzer.compute_total_depth() > 0
    summary = analyzer.summarize()
    assert summary.quality in {"EXCELLENT", "GOOD", "ACCEPTABLE"}


def test_orderbook_handles_empty_payload() -> None:
    analyzer = OrderbookAnalyzer({})
    assert analyzer.compute_best_bid() is None
    assert analyzer.classify_orderbook_quality() == "INSUFFICIENT_DATA"


def test_estimate_slippage_for_size_uses_levels() -> None:
    analyzer = OrderbookAnalyzer(SAMPLE)
    slippage = analyzer.estimate_slippage_for_size(2_000)
    assert slippage >= 0
    assert slippage < 0.5
