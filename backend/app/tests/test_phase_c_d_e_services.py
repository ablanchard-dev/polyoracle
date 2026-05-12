"""Tests for Phase C/D/E services prep (2026-05-11).

Couvre :
  - wallet_weighting_engine (M3)
  - growth_metrics_engine (Sharpe/Sortino/Calmar)
  - vault_manager (HWM vault)
  - state_recovery (D3 partial)
  - trade_quality_score (pre/post)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.audit import AuditEvent  # noqa: F401 — for create_all
from app.models.bot import BotState
from app.models.trade import PaperTrade


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(BotState(id=1, mode="PAPER", running=True, paper_capital=100.0))
        s.commit()
        yield s


def _seed_closed_trade(
    session, *, idx: int, wallet: str, pnl: float, notional: float = 5.0,
    minutes_ago: int = 60, holding_min: int = 10,
):
    closed = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    opened = closed - timedelta(minutes=holding_min)
    session.add(PaperTrade(
        id=f"pt-{idx}",
        market_id=f"0xmkt-{idx}",
        outcome="Yes",
        side="BUY",
        quantity=notional / 0.5,
        average_price=0.5,
        notional_usd=notional,
        realized_pnl=pnl,
        status="closed",
        opened_at=opened,
        closed_at=closed,
        wallet_address=wallet.lower(),
    ))
    session.commit()


# ====================== M3 wallet weighting ======================


def test_m3_wallet_weight_low_sample_returns_neutral(session):
    from app.services.wallet_weighting_engine import compute_wallet_weights, BASE_PRIOR_NEUTRAL
    # Only 5 trades — below MIN_RECENT_FOR_BOOST (30) → neutral weight 1.0
    for i in range(5):
        _seed_closed_trade(session, idx=i, wallet="0xelite1", pnl=0.5)
    weights = compute_wallet_weights(session)
    assert "0xelite1" in weights
    w = weights["0xelite1"]
    assert w.regularized_weight == BASE_PRIOR_NEUTRAL
    assert w.n_recent == 5
    assert any("low_sample" in n for n in w.notes)


def test_m3_wallet_weight_clamp_paper_mode(session):
    from app.services.wallet_weighting_engine import (
        compute_wallet_weights, PAPER_CAP_MIN, PAPER_CAP_MAX,
    )
    # Many strong trades → weight should be clamped to PAPER_CAP_MAX = 1.5
    for i in range(50):
        _seed_closed_trade(session, idx=i, wallet="0xelite1", pnl=2.0)
    weights = compute_wallet_weights(session, paper_mode=True)
    w = weights["0xelite1"]
    assert PAPER_CAP_MIN <= w.final_weight <= PAPER_CAP_MAX


def test_m3_distribution_payload(session):
    from app.services.wallet_weighting_engine import wallet_weights_payload
    for i in range(35):
        _seed_closed_trade(session, idx=i, wallet="0xelite1", pnl=0.3)
    payload = wallet_weights_payload(session)
    assert payload["n_wallets"] == 1
    assert payload["weight_distribution"]["min"] is not None
    assert payload["weight_distribution"]["max"] is not None


# ====================== Growth metrics ======================


def test_growth_unknown_classification_with_no_trades(session):
    from app.services.growth_metrics_engine import compute_growth_health_report
    r = compute_growth_health_report(session, window_hours=24.0)
    assert r.classification == "UNKNOWN"
    assert r.sample_size == 0


def test_growth_healthy_classification(session):
    from app.services.growth_metrics_engine import compute_growth_health_report
    # Generate 30 trades over ~7 days, mostly winning
    for i in range(30):
        _seed_closed_trade(
            session, idx=i, wallet="0xelite1",
            pnl=1.0 if i % 3 != 0 else -0.5,
            minutes_ago=i * 60 * 5,  # spread over 150 hours = 6.25 days
            holding_min=15,
        )
    r = compute_growth_health_report(session, window_hours=240.0)
    assert r.sample_size == 30
    assert r.win_rate is not None and r.win_rate > 0.6
    assert r.profit_factor is not None and r.profit_factor > 1


def test_growth_drawdown_detection(session):
    from app.services.growth_metrics_engine import compute_growth_health_report
    # 5 wins then 10 big losses
    for i in range(5):
        _seed_closed_trade(session, idx=i, wallet="0x1", pnl=2.0, minutes_ago=240 - i)
    for i in range(10):
        _seed_closed_trade(session, idx=10 + i, wallet="0x1", pnl=-3.0, minutes_ago=200 - i)
    r = compute_growth_health_report(session, window_hours=24.0)
    assert r.max_drawdown_pct is not None and r.max_drawdown_pct > 0


# ====================== Vault manager ======================


def test_vault_initial_state():
    from app.services.vault_manager import initial_state
    s = initial_state(100.0)
    assert s.hwm == 100.0
    assert s.active_capital == 100.0
    assert s.vault_balance == 0.0
    assert s.sizing_mode == "FULL"


def test_vault_trigger_at_20pct_gain():
    from app.services.vault_manager import initial_state, vault_step, VAULT_QUANTUM_PCT
    s = initial_state(100.0)
    # +20% = trigger at 120 total equity → transfer 50% × 20 gain = 10
    decision = vault_step(s, current_pnl_realized_total=20.0)
    assert decision.transfer_triggered is True
    assert decision.transfer_amount == round(VAULT_QUANTUM_PCT * 20.0, 4)
    assert decision.state_after.vault_balance > 0
    assert decision.state_after.active_capital < 120.0  # transferred to vault


def test_vault_no_trigger_below_threshold():
    from app.services.vault_manager import initial_state, vault_step
    s = initial_state(100.0)
    # +10% = NOT yet at 20% trigger
    decision = vault_step(s, current_pnl_realized_total=10.0)
    assert decision.transfer_triggered is False
    assert decision.transfer_amount == 0.0


def test_vault_sizing_mode_changes_with_dd():
    from app.services.vault_manager import initial_state, vault_step
    s = initial_state(100.0)
    # First push HWM to 150 then drop -25% → PAUSE
    s = vault_step(s, current_pnl_realized_total=50.0).state_after
    # Now active is ~100 (50 transferred, vault=25), HWM=150
    # Simulate big loss
    decision2 = vault_step(s, current_pnl_realized_total=-50.0)
    # active becomes ~50, total=75, hwm stays 150, dd=(150-75)/150 = 0.5 → PAUSED
    assert decision2.state_after.sizing_mode == "PAUSED"


def test_vault_simulation_30days_growth_pattern():
    from app.services.vault_manager import simulate_trajectory
    # Simule 30 jours +10% chacun (croissance constante)
    rows = simulate_trajectory([0.10] * 30, starting_capital=100.0)
    assert len(rows) == 31  # day 0 + 30 days
    # Vault balance should grow over time as triggers fire
    assert rows[-1]["vault"] > 0
    # Active capital should be lower than total equity (vault deducted)
    assert rows[-1]["active"] < rows[-1]["total"]


# ====================== State recovery ======================


def test_state_recovery_audit_no_open_positions(session):
    from app.services.state_recovery import audit_open_positions_at_boot
    audit = audit_open_positions_at_boot(session)
    assert audit.open_positions_count == 0
    assert audit.orphan_count == 0
    assert audit.stale_count == 0
    assert "clean boot" in audit.recommendations[0].lower()


def test_state_recovery_detects_orphans(session):
    from app.services.state_recovery import audit_open_positions_at_boot
    # Open a trade 8 days ago — orphan
    old_open = datetime.now(UTC) - timedelta(days=8)
    session.add(PaperTrade(
        id="orphan-1", market_id="0xa", outcome="Yes", side="BUY",
        quantity=10, average_price=0.5, status="open", opened_at=old_open,
        notional_usd=5.0,
    ))
    session.commit()
    audit = audit_open_positions_at_boot(session)
    assert audit.open_positions_count == 1
    assert audit.orphan_count == 1
    assert any("orphan" in r.lower() for r in audit.recommendations)


def test_state_recovery_reschedule_count_only(session):
    """Without scheduler instance, counts open positions only."""
    from app.services.state_recovery import reschedule_open_positions_into_closeloop
    for i in range(3):
        session.add(PaperTrade(
            id=f"open-{i}", market_id=f"0x{i}", outcome="Yes", side="BUY",
            quantity=10, average_price=0.5, status="open",
            opened_at=datetime.now(UTC) - timedelta(hours=1),
        ))
    session.commit()
    n = reschedule_open_positions_into_closeloop(session, scheduler=None)
    assert n == 3


def test_state_recovery_emergency_close_orphans(session):
    """Emergency close marks orphans as closed with EMERGENCY_RECOVERY_BOOT reason."""
    from app.services.state_recovery import emergency_close_orphans, EMERGENCY_CLOSE_REASON
    old_open = datetime.now(UTC) - timedelta(days=10)
    session.add(PaperTrade(
        id="orphan-1", market_id="0xa", outcome="Yes", side="BUY",
        quantity=10, average_price=0.5, status="open", opened_at=old_open,
        notional_usd=5.0,
    ))
    session.commit()
    n = emergency_close_orphans(session)
    assert n == 1
    closed = session.get(PaperTrade, "orphan-1")
    assert closed.status == "closed"
    assert closed.close_reason == EMERGENCY_CLOSE_REASON
    assert closed.realized_pnl == 0.0  # neutral exit


# ====================== Trade quality score ======================


def test_quality_pre_score_high_input():
    from app.services.trade_quality_score import (
        PreTradeInputs, compute_pre_trade_score,
    )
    pre = PreTradeInputs(
        wallet_score=90, copy_efficiency_wallet=0.85,
        freshness_seconds=10, spread=0.01, liquidity_score=80,
        copyable_edge=85, expected_rr=2.0,
        duration_bucket="ULTRA_SHORT", category="politics",
        cluster_confidence=0.8,
    )
    score, breakdown = compute_pre_trade_score(pre)
    assert score >= 70  # all high inputs → high score
    assert sum(breakdown.values()) == score


def test_quality_pre_score_low_input_recommends_watch():
    from app.services.trade_quality_score import (
        PreTradeInputs, evaluate_trade_quality,
    )
    pre = PreTradeInputs(
        wallet_score=20, copy_efficiency_wallet=0.3,
        freshness_seconds=200, spread=0.08, liquidity_score=20,
        copyable_edge=20, expected_rr=0.5,
        duration_bucket="LONG", category="unknown",
        cluster_confidence=0.1,
    )
    result = evaluate_trade_quality(pre)
    assert result.open_recommendation == "WATCH"
    assert result.pre_trade_score < 60


def test_quality_post_close_score_with_realized_r():
    from app.services.trade_quality_score import (
        PostCloseInputs, compute_post_close_score,
    )
    post = PostCloseInputs(
        realized_r=1.5, expected_slippage=0.01, actual_slippage=0.012,
        expected_copy_delay_s=10, actual_copy_delay_s=12,
        fill_quality_pct=0.95, bot_realized_r=1.5, source_wallet_realized_r=2.0,
    )
    score, breakdown = compute_post_close_score(post)
    assert 50 <= score <= 100
    assert sum(breakdown.values()) == score


def test_quality_pre_post_gap_negative_when_overestimated():
    from app.services.trade_quality_score import (
        PreTradeInputs, PostCloseInputs, evaluate_trade_quality,
    )
    pre = PreTradeInputs(
        wallet_score=95, copy_efficiency_wallet=0.95,
        freshness_seconds=5, spread=0.005, liquidity_score=95,
        copyable_edge=90, expected_rr=2.5,
        duration_bucket="ULTRA_SHORT", category="politics",
        cluster_confidence=0.9,
    )
    post = PostCloseInputs(
        realized_r=-1.0, expected_slippage=0.005, actual_slippage=0.05,
        expected_copy_delay_s=5, actual_copy_delay_s=60,
        fill_quality_pct=0.40, bot_realized_r=-1.0, source_wallet_realized_r=2.0,
    )
    result = evaluate_trade_quality(pre, post)
    assert result.pre_post_gap is not None
    assert result.pre_post_gap < 0  # post much lower than pre
    assert any("overestimated" in n for n in result.notes)
