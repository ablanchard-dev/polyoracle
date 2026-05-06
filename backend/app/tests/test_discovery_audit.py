"""Tests for the v0.4.2 discovery audit pipeline.

These run on the in-process FastAPI app via TestClient so we exercise the
full route → service → SmartWalletAuditor → WinRateEngine → CSV/JSON path.

We force ``polymarket_public_enabled=False`` so the audit uses the mock
fallback (no network) but the discovery report still exercises every code
path (export files written, conclusion derived, win-rate breakdown wired
into SmartWalletScore at the capped 10% weight).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.database import get_session
from app.main import app
from app.services.discovery_audit_service import DiscoveryAuditService
from app.services.smart_wallet_auditor import SmartWalletAuditor
from app.services.win_rate_engine import WinRateBreakdown


@pytest.fixture()
def app_client(tmp_path):
    settings = get_settings()
    settings.polymarket_public_enabled = False
    settings.mock_data_enabled = True
    # Redirect exports to the test tmp path so we don't pollute real /data.
    settings.sqlite_path = str(tmp_path / "discovery.db")
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401

    engine = create_engine(f"sqlite:///{(tmp_path / 'discovery.db').as_posix()}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as client:
        yield client, tmp_path
    app.dependency_overrides.pop(get_session, None)


def test_discovery_audit_writes_csv_and_json_and_returns_conclusion(app_client) -> None:
    client, tmp_path = app_client
    response = client.post("/wallets/discovery/audit", json={"limit": 10, "batch_size": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["audited_wallets_count"] >= 1
    assert body["conclusion"] in {
        "DISCOVERY_GOOD",
        "DISCOVERY_VOLUME_BIASED",
        "DISCOVERY_TOO_MUCH_MOCK",
        "DISCOVERY_INSUFFICIENT_DATA",
        "DISCOVERY_API_UNRELIABLE",
        "DISCOVERY_NEEDS_ALTERNATIVE_METHOD",
    }
    csv_path = Path(body["csv_path"])
    json_path = Path(body["json_path"])
    assert csv_path.exists()
    assert json_path.exists()
    rows = list(csv.DictReader(csv_path.open("r", encoding="utf-8")))
    assert rows
    sample = rows[0]
    for column in (
        "rank",
        "address",
        "final_score",
        "tier",
        "resolved_market_win_rate",
        "win_rate_confidence",
    ):
        assert column in sample


def test_get_latest_discovery_audit_returns_payload(app_client) -> None:
    client, _ = app_client
    client.post("/wallets/discovery/audit", json={"limit": 5})
    response = client.get("/wallets/discovery/audit")
    assert response.status_code == 200
    body = response.json()
    assert "conclusion" in body
    assert "audited_wallets_count" in body


def test_winrate_summary_endpoint(app_client) -> None:
    client, _ = app_client
    client.post("/wallets/discovery/audit", json={"limit": 5})
    response = client.get("/wallets/winrate/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert "win_rate_confidence_breakdown" in body


def test_no_silent_mock_fallback_when_mock_disabled(tmp_path) -> None:
    """If both ``polymarket_public_enabled`` is True and ``mock_data_enabled`` is
    False AND the public API call fails, the discovery must report
    ``data_source="unavailable"`` instead of silently switching to mock data."""
    settings = get_settings()
    settings.polymarket_public_enabled = True
    settings.mock_data_enabled = False

    engine = create_engine(f"sqlite:///{(tmp_path / 'noop.db').as_posix()}", connect_args={"check_same_thread": False})
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            auditor = SmartWalletAuditor(session)

            def _explode(*args, **kwargs):
                raise RuntimeError("simulated network outage")

            auditor._discover_via_recent_trades = _explode  # type: ignore[assignment]
            # Also force the legacy path to fail.
            class _NoLeaderboard:
                def fetch_leaderboard(self) -> list:
                    raise RuntimeError("404")
            auditor._data_client = lambda: _NoLeaderboard()  # type: ignore[assignment]

            result = auditor.discover_top_wallets_detailed(limit=10)
            assert result["data_source"] == "unavailable"
            assert result["wallets"] == []
    finally:
        # Reset settings flags so other tests aren't affected.
        settings.mock_data_enabled = True


def test_win_rate_score_capped_and_zero_for_insufficient_data(tmp_path) -> None:
    """A wallet with INSUFFICIENT_DATA win rate must contribute 0 to the score, and
    even a 100% HIGH-confidence rate cannot push the total above 10 points."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'cap.db').as_posix()}", connect_args={"check_same_thread": False})
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        auditor = SmartWalletAuditor(session)

        # 1. Insufficient sample → 0.
        zero, meta = auditor._compute_win_rate_score(None)
        assert zero == 0.0
        assert meta["confidence"] == "INSUFFICIENT_DATA"

        # 2. HIGH confidence, 100% rate → exactly 10.
        breakdown = WinRateBreakdown(
            address="0xperfect",
            resolved_market_win_rate=1.0,
            trade_level_win_rate=1.0,
            market_sample_size=200,
            resolved_winning_markets=200,
            resolved_losing_markets=0,
            unresolved_markets_count=0,
            win_rate_confidence="HIGH",
            win_rate_warnings=[],
            data_source="polymarket_data+gamma",
            explanation="",
        )
        score, _ = auditor._compute_win_rate_score(breakdown)
        assert score == 10.0

        # 3. LOW confidence + 100% rate must NOT promote the wallet to ELITE on win rate alone.
        low = WinRateBreakdown(
            address="0xlow",
            resolved_market_win_rate=1.0,
            trade_level_win_rate=1.0,
            market_sample_size=15,
            resolved_winning_markets=15,
            resolved_losing_markets=0,
            unresolved_markets_count=0,
            win_rate_confidence="LOW",
            win_rate_warnings=["high_win_rate_small_sample"],
            data_source="polymarket_data+gamma",
            explanation="",
        )
        score_low, _ = auditor._compute_win_rate_score(low)
        assert score_low <= 3.0  # capped at 30% of the 10pt budget for LOW confidence
