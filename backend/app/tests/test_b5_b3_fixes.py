"""v0.7.2 B5 + B3 fix tests.

* B5 — `wallet_polling_engine.load_cohort()` reads
  `validated_paper_universe_latest.csv` first AND intersects with
  ``MarketFirstWalletRecord.candidate_status IN ('ELITE', 'STRONG')`` so
  the polling cohort excludes wallets demoted between syncs.

* B3 — `presets.yaml` BASE_PAPER and BASE_LIVE got their
  ``min_copyable_edge`` thresholds relaxed (0.40→0.15 paper, 0.50→0.25
  live) to match the real distribution of audit signals (median ~0.20).
"""

from __future__ import annotations

import csv
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.models.wallet import MarketFirstWalletRecord
from app.services.wallet_polling_engine import WalletPollingEngine


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        if not rows:
            return
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ============ B5 ============


def test_b5_load_cohort_excludes_dropped_wallets(tmp_path: Path) -> None:
    """When MFWR knows about wallets, only ELITE/STRONG remain after filter."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            MarketFirstWalletRecord(address="0xelite", candidate_status="ELITE"),
            MarketFirstWalletRecord(address="0xstrong", candidate_status="STRONG"),
            MarketFirstWalletRecord(address="0xdropped", candidate_status="DROPPED"),
            MarketFirstWalletRecord(address="0xoutlier", candidate_status="OUTLIER_FLAGGED"),
            MarketFirstWalletRecord(address="0xfailed", candidate_status="FAILED_VALIDATION"),
            MarketFirstWalletRecord(address="0xcand", candidate_status="CANDIDATE_ELITE"),
        ])
        session.commit()

    csv_path = tmp_path / "universe.csv"
    _write_csv(csv_path, [
        {"address": "0xelite", "allowed_aggressive": "True"},
        {"address": "0xstrong", "allowed_aggressive": "True"},
        {"address": "0xdropped", "allowed_aggressive": "True"},
        {"address": "0xoutlier", "allowed_aggressive": "True"},
        {"address": "0xfailed", "allowed_aggressive": "True"},
        {"address": "0xcand", "allowed_aggressive": "True"},
    ])

    with Session(engine) as session:
        cohort = WalletPollingEngine.load_cohort(path=csv_path, session=session)
    # CANDIDATE_ELITE / DROPPED / OUTLIER / FAILED all excluded by the
    # MFWR.candidate_status IN ('ELITE','STRONG') filter.
    assert sorted(cohort) == ["0xelite", "0xstrong"]


def test_b5_load_cohort_without_session_returns_csv_subset(tmp_path: Path) -> None:
    """If no session is supplied (legacy callers, tests), the cohort is the
    raw CSV ``allowed_aggressive=True`` subset — no DB filter applied."""
    csv_path = tmp_path / "universe.csv"
    _write_csv(csv_path, [
        {"address": "0xa", "allowed_aggressive": "True"},
        {"address": "0xb", "allowed_aggressive": "False"},
        {"address": "0xc", "allowed_aggressive": "True"},
    ])
    cohort = WalletPollingEngine.load_cohort(path=csv_path)
    assert sorted(cohort) == ["0xa", "0xc"]


def test_b5_load_cohort_handles_empty_mfwr_gracefully(tmp_path: Path) -> None:
    """If MFWR is empty (e.g. fresh test DB), filter is a no-op (no wallets
    match → cohort stays raw). The fix prefers existing CSV data over an
    empty MFWR rather than returning [] silently."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    csv_path = tmp_path / "universe.csv"
    _write_csv(csv_path, [
        {"address": "0xa", "allowed_aggressive": "True"},
    ])
    with Session(engine) as session:
        cohort = WalletPollingEngine.load_cohort(path=csv_path, session=session)
    # No tradable rows in MFWR → filter sees empty set → keeps CSV-derived
    # cohort intact (the conservative default; logged as warning).
    assert cohort == ["0xa"]


def test_b5_load_cohort_lowercases_addresses(tmp_path: Path) -> None:
    csv_path = tmp_path / "universe.csv"
    _write_csv(csv_path, [
        {"address": "0xELITE", "allowed_aggressive": "True"},
    ])
    cohort = WalletPollingEngine.load_cohort(path=csv_path)
    assert cohort == ["0xelite"]


# ============ B3 ============


def test_b3_base_paper_min_edge_relaxed_to_015():
    from app.services.preset_loader import get_preset
    p = get_preset("BASE_PAPER")
    assert p.min_copyable_edge == 0.15


def test_b3_base_live_min_edge_relaxed_to_025():
    from app.services.preset_loader import get_preset
    p = get_preset("BASE_LIVE")
    assert p.min_copyable_edge == 0.25


def test_b3_signal_with_edge_020_now_passes_paper_gate():
    """Median observed edge 0.20 should pass BASE_PAPER's 0.15 gate."""
    from dataclasses import replace
    from app.services.capital_allocator import (
        AllocatorParams,
        CapitalAllocator,
        PortfolioState,
        TradeSignal,
    )
    from app.services.preset_loader import get_preset
    base_paper = get_preset("BASE_PAPER")
    # Disable capital_aware so we test the raw preset only.
    p = replace(base_paper, capital_aware_thresholds=False, polymarket_fee_pct=0.0, min_edge_after_fees=0.0)
    sig = TradeSignal(
        wallet_address="0xelite",
        wallet_tier="ELITE",
        market_id="m1",
        side="BUY",
        price=0.5,
        size=10.0,
        notional_usd=10.0,
        ev_lower_bound=0.10,
        copyable_edge=0.20,  # median observed
        spread=0.01,
        liquidity=10000,
        liquidity_score=80,
        orderbook_quality="OK",
        category="political",
        copy_delay_seconds=10.0,
    )
    state = PortfolioState(capital_total=1000.0, capital_available=900.0)
    d = CapitalAllocator().evaluate_trade(sig, state, p)
    assert d.accepted is True


def test_b3_edge_below_015_still_blocked_paper():
    """Edge 0.10 must still REJECT (we don't trade noise)."""
    from dataclasses import replace
    from app.services.capital_allocator import (
        CapitalAllocator,
        PortfolioState,
        TradeSignal,
    )
    from app.services.preset_loader import get_preset
    base_paper = get_preset("BASE_PAPER")
    p = replace(base_paper, capital_aware_thresholds=False, polymarket_fee_pct=0.0, min_edge_after_fees=0.0)
    sig = TradeSignal(
        wallet_address="0xelite",
        wallet_tier="ELITE",
        market_id="m1",
        side="BUY",
        price=0.5,
        size=10.0,
        notional_usd=10.0,
        ev_lower_bound=0.10,
        copyable_edge=0.10,  # below 0.15 gate
        spread=0.01,
        liquidity=10000,
        liquidity_score=80,
        orderbook_quality="OK",
        category="political",
        copy_delay_seconds=10.0,
    )
    state = PortfolioState(capital_total=1000.0, capital_available=900.0)
    d = CapitalAllocator().evaluate_trade(sig, state, p)
    assert d.accepted is False
    assert d.reason_code == "LOW_EDGE"
