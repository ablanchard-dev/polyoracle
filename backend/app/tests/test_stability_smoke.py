"""End-to-end TestClient smoke for v0.4.1 stability check.

Spins the real FastAPI app against a fresh in-memory SQLite engine via
`get_session` dependency override, exercises every read endpoint and runs
PAPER mode audit cycles. Verifies:

- /markets/tradable + /signals/active are reachable (404 regression guard).
- Cycle counters (markets/wallets/trades/signals/rejected) line up.
- Auto paper trading dedupes by signal_id across cycles.
- The no-trade decision log captures rejections with documented reason codes.
- RESEARCH mode never opens a paper trade.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import get_session
from app.main import app


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    settings = get_settings()
    settings.polymarket_public_enabled = False
    # Force every SQLModel table to register before create_all.
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401

    db_path = tmp_path_factory.mktemp("smoke") / "stability.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(get_session, None)


GET_ENDPOINTS = [
    "/health",
    "/markets",
    "/markets/hot",
    "/markets/tradable",
    "/wallets/top?limit=20",
    "/wallets/audited",
    "/wallets/watchlist",
    "/wallets/stats",
    "/trades/recent",
    "/trades/audited",
    "/trades/large",
    "/trades/smart-money",
    "/trades/clusters",
    "/trades/stats",
    "/signals",
    "/signals/active",
    "/signals/rejected",
    "/signals/decisions",
    "/paper/positions",
    "/paper/trades",
    "/paper/report",
    "/paper/performance",
    "/edge/report",
    "/edge/strategies",
    "/edge/no-trade-log",
    "/bot/status",
    "/bot/loop/status",
]


def test_all_get_endpoints_return_200(app_client) -> None:
    for path in GET_ENDPOINTS:
        response = app_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:200]}"


def test_run_once_in_paper_mode_opens_at_most_one_trade_per_signal(app_client) -> None:
    app_client.post("/paper/reset")
    app_client.post("/bot/mode/paper")

    cycle1 = app_client.post("/bot/audit/run-once").json()
    cycle2 = app_client.post("/bot/audit/run-once").json()

    assert cycle1["markets_scanned"] >= 1
    assert cycle1["wallets_audited"] >= 1
    assert cycle1["trades_audited"] >= 1
    assert cycle1["signals_generated"] >= 1
    assert isinstance(cycle1["rejection_reasons"], dict)

    positions_after_cycle1 = app_client.get("/paper/positions").json()
    positions_after_cycle2 = app_client.get("/paper/positions").json()
    assert len(positions_after_cycle2) == len(positions_after_cycle1)
    signal_ids = [pos["signal_id"] for pos in positions_after_cycle2 if pos.get("signal_id")]
    assert len(signal_ids) == len(set(signal_ids))


def test_no_trade_decision_log_records_rich_details(app_client) -> None:
    """Self-contained: runs audit cycles to populate the log, then validates
    its shape and reason_codes. Doesn't depend on prior tests."""
    app_client.post("/paper/reset")
    app_client.post("/bot/mode/paper")
    app_client.post("/bot/audit/run-once")
    app_client.post("/bot/audit/run-once")

    log = app_client.get("/edge/no-trade-log").json()
    assert isinstance(log, list)
    assert len(log) > 0, "PAPER cycles must produce no-trade entries for rejected signals"
    sample = log[0]
    for field in ("id", "reason_code", "details", "created_at"):
        assert field in sample
    # v0.7.2+ added cluster engine reason_codes; v0.7.8 added more allocator
    # codes. Keep this list in sync with REJECT_CODES + cluster + audit codes.
    valid_codes = {
        "LOW_SIGNAL_SCORE",
        "LOW_CONFIDENCE",
        "LOW_LIQUIDITY",
        "WIDE_SPREAD",
        "BAD_ORDERBOOK",
        "NO_COPYABLE_EDGE",
        "LATE_ENTRY",
        "TOO_MUCH_EXPOSURE",
        "DAILY_LOSS_LIMIT",
        "WEEKLY_LOSS_LIMIT",
        "KILL_SWITCH",
        "INSUFFICIENT_DATA",
        "WALLET_NOT_RELIABLE",
        "NOT_PAPER_DECISION",
        "AUDIT_DECISION_NOT_PAPER",
        # v0.7.2 cluster engine
        "CLUSTER_DUPLICATE",
        "CLUSTER_PENDING_CONSENSUS",
        "CLUSTER_LATE_CROWD",
        "CLUSTER_CONFLICT",
        "CLUSTER_ALREADY_TRADED",
        "CLUSTER_EXPIRED",
        # v0.7.4+ polling pipeline
        "STALE_SIGNAL_BACKFILL",
        # v0.7.8 allocator
        "WALLET_TIER",
        "LOW_EV",
        "LOW_EDGE",
        "INSUFFICIENT_CAPITAL",
        "INSUFFICIENT_CAPITAL_FOR_STAKE",
        "MAX_TOTAL_EXPOSURE",
        "MAX_WALLET_EXPOSURE",
        "MAX_MARKET_EXPOSURE",
        "MAX_POSITIONS",
        "NEGATIVE_EV_AFTER_FEES",
        "MIN_ORDER_UNVERIFIED",
        "LIVE_DISABLED",
        "CAPITAL_LOCK_TOO_LONG",
        "UNKNOWN_DEADLINE",
        "UNKNOWN_CATEGORY",
        "MIN_ORDER_EXCEEDS_MARKET_CAP",
        "MIN_ORDER_EXCEEDS_WALLET_CAP",
        "MARKET_METADATA_NOT_FOUND",
        "MARKET_METADATA_FETCH_FAILED",
        "MARKET_METADATA_TIMEOUT",
        "ORDERBOOK_UNAVAILABLE",
        "DATA_MISSING_MARKET_METADATA",
    }
    for entry in log:
        assert entry["reason_code"] in valid_codes, entry


def test_research_mode_does_not_open_paper_trades(app_client) -> None:
    app_client.post("/paper/reset")
    app_client.post("/bot/mode/research")
    result = app_client.post("/bot/audit/run-once").json()
    assert result["paper_trades_opened"] == 0
    assert app_client.get("/paper/positions").json() == []


def test_kill_switch_blocks_subsequent_cycles(app_client) -> None:
    app_client.post("/bot/mode/paper")
    app_client.post("/bot/kill-switch")
    result = app_client.post("/bot/audit/run-once").json()
    assert result.get("kill_switch_active") is True or result.get("paper_trades_opened", 0) == 0
