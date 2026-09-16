"""MOCK_DATA_ENABLED=false must really mean: no fake markets.

Before: when the Gamma API failed, `fetch_active_markets` upserted `mock_markets()`
into the markets table whatever the flag said (only the snapshot file was gated).
With mocks disabled, an API outage silently mixed fake markets with real ones and
the cycle reported them as scanned markets.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _scanner(mock_enabled: bool):
    from app.services.market_scanner import MarketScanner

    s = MarketScanner.__new__(MarketScanner)
    s.settings = MagicMock(polymarket_public_enabled=True, mock_data_enabled=mock_enabled, market_fetch_limit=10)
    s.upsert_markets = MagicMock()
    s.save_market_snapshot = MagicMock()
    return s


def test_gamma_failure_with_mocks_disabled_raises_and_writes_nothing():
    # Raise, not []: an empty list reads as "0 markets" with no reason, while the
    # bot loop turns an exception into the cycle's visible `last_error`.
    import pytest

    s = _scanner(mock_enabled=False)
    with patch("app.services.market_scanner.GammaClient") as gamma:
        gamma.return_value.fetch_active_markets.side_effect = RuntimeError("gamma down")
        with pytest.raises(RuntimeError, match="gamma down"):
            s.fetch_active_markets()
    s.upsert_markets.assert_not_called()
    s.save_market_snapshot.assert_not_called()


def test_gamma_failure_with_mocks_enabled_still_falls_back_to_mock():
    s = _scanner(mock_enabled=True)
    with patch("app.services.market_scanner.GammaClient") as gamma:
        gamma.return_value.fetch_active_markets.side_effect = RuntimeError("gamma down")
        markets = s.fetch_active_markets()
    assert markets and all(m.id.startswith("mock-") for m in markets)
    s.save_market_snapshot.assert_called_once()
