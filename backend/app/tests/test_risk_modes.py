"""Tests for the v0.5.2 risk-mode policy.

Locks in the rules from the spec:

- SAFE rejects CANDIDATE_ELITE, accepts STRONG/ELITE only.
- AGGRESSIVE accepts CANDIDATE_ELITE iff sample size ≥ 20 resolved markets.
- FULL_PAPER has no daily trade-count cap, but the per-trade / per-wallet /
  per-market / total exposure caps and the kill switch always apply.
- Live execution is permanently blocked at the profile level
  (``live_allowed=False`` for every shipped profile).
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.models.bot import BotState
from app.models.signal import Signal, SignalType
from app.models.wallet import MarketFirstWalletRecord
from app.services.paper_trading_engine import PaperTradingEngine
from app.services.risk_engine import RiskEngine
from app.services.risk_mode import (
    AGGRESSIVE,
    FULL_PAPER,
    SAFE,
    get_profile,
    list_profiles,
)
from app.services.risk_mode_service import (
    get_active_mode,
    get_active_profile,
    set_active_mode,
)


def _engine() -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401
    SQLModel.metadata.create_all(engine)
    return engine


# ---------------- profile registry ----------------


def test_registry_returns_three_profiles_and_safe_is_default() -> None:
    profiles = list_profiles()
    names = {p["name"] for p in profiles}
    assert names == {"SAFE", "AGGRESSIVE", "FULL_PAPER"}
    assert get_profile(None).name == "SAFE"
    assert get_profile("safe").name == "SAFE"
    assert get_profile("FULL_PAPER").name == "FULL_PAPER"


def test_no_profile_allows_live_execution() -> None:
    for profile in (SAFE, AGGRESSIVE, FULL_PAPER):
        assert profile.live_allowed is False, f"{profile.name} must not allow live execution"


def test_full_paper_has_no_daily_trade_count_limit_but_keeps_exposure_caps() -> None:
    assert FULL_PAPER.max_daily_trades is None
    assert FULL_PAPER.no_daily_trade_count_limit is True
    # Exposure caps still apply.
    assert FULL_PAPER.max_total_exposure > 0
    assert FULL_PAPER.max_wallet_exposure > 0
    assert FULL_PAPER.max_market_exposure > 0
    assert FULL_PAPER.max_risk_per_trade > 0


# ---------------- mode persistence ----------------


def test_set_active_mode_round_trip() -> None:
    engine = _engine()
    with Session(engine) as session:
        assert get_active_mode(session) == "SAFE"
        set_active_mode(session, "FULL_PAPER", by="test")
        assert get_active_mode(session) == "FULL_PAPER"
        assert get_active_profile(session).name == "FULL_PAPER"
        # Reject unknown mode.
        try:
            set_active_mode(session, "WILD")
        except ValueError as exc:
            assert "Unknown" in str(exc)
        else:
            raise AssertionError("unknown mode must raise ValueError")


# ---------------- RiskEngine profile enforcement ----------------


def _ctx(**overrides: object) -> dict:
    base = dict(
        candidate_status="STRONG",
        market_sample_size=40,
        win_rate_confidence="MEDIUM",
        signal_score=85,
        spread=0.01,
        liquidity=10_000.0,
        copyable_edge_score=70.0,
        orderbook_quality="GOOD",
        copy_delay_seconds=30.0,
        price_deterioration=0.0,
        total_exposure=0.0,
        market_exposure=0.0,
        wallet_exposure=0.0,
        open_positions_count=0,
        daily_trades_count=0,
        kill_switch_active=False,
    )
    base.update(overrides)
    return base


def test_safe_refuses_candidate_elite() -> None:
    risk = RiskEngine()
    decision = risk.validate_for_mode(SAFE, **_ctx(candidate_status="CANDIDATE_ELITE"))
    assert decision.approved is False
    assert decision.reason_code == "WALLET_NOT_RELIABLE"


def test_aggressive_accepts_candidate_elite_with_sample_at_least_20() -> None:
    risk = RiskEngine()
    ok = risk.validate_for_mode(AGGRESSIVE, **_ctx(candidate_status="CANDIDATE_ELITE", market_sample_size=22))
    assert ok.approved is True

    nope = risk.validate_for_mode(AGGRESSIVE, **_ctx(candidate_status="CANDIDATE_ELITE", market_sample_size=15))
    assert nope.approved is False
    assert nope.reason_code == "INSUFFICIENT_DATA"


def test_full_paper_has_no_daily_trade_cap_but_caps_total_exposure() -> None:
    risk = RiskEngine()
    # 1000 trades today: still passes the daily-count gate because FULL_PAPER has no cap.
    decision = risk.validate_for_mode(FULL_PAPER, **_ctx(daily_trades_count=1_000))
    assert decision.approved is True

    # Total exposure above the FULL cap: blocked.
    blocked = risk.validate_for_mode(FULL_PAPER, **_ctx(total_exposure=0.50))
    assert blocked.approved is False
    assert blocked.reason_code == "TOO_MUCH_EXPOSURE"


def test_safe_caps_open_positions() -> None:
    risk = RiskEngine()
    blocked = risk.validate_for_mode(SAFE, **_ctx(open_positions_count=5))
    assert blocked.approved is False
    assert blocked.reason_code == "TOO_MUCH_EXPOSURE"


def test_kill_switch_overrides_every_profile() -> None:
    risk = RiskEngine()
    for profile in (SAFE, AGGRESSIVE, FULL_PAPER):
        decision = risk.validate_for_mode(profile, **_ctx(kill_switch_active=True))
        assert decision.approved is False
        assert decision.reason_code == "KILL_SWITCH"


def test_safe_requires_medium_high_confidence() -> None:
    risk = RiskEngine()
    blocked = risk.validate_for_mode(SAFE, **_ctx(win_rate_confidence="LOW"))
    assert blocked.approved is False
    assert blocked.reason_code == "LOW_CONFIDENCE"

    # AGGRESSIVE does NOT require it.
    ok = risk.validate_for_mode(AGGRESSIVE, **_ctx(win_rate_confidence="LOW"))
    assert ok.approved is True


# ---------------- PaperTradingEngine integration ----------------


def _seed_wallet(session: Session, address: str, *, status: str, sample: int) -> None:
    record = MarketFirstWalletRecord(
        address=address.lower(),
        market_first_score=80,
        composite_score=85,
        tier="STRONG",
        status="STRONG_AND_ACTIVE",
        candidate_status=status,
        resolved_market_win_rate=0.85,
        win_rate_confidence="MEDIUM",
        resolved_markets_traded=sample,
        resolved_winning_markets=int(sample * 0.85),
        resolved_losing_markets=int(sample * 0.15),
        copyability_score=80,
        average_position_size=500,
    )
    session.merge(record)
    session.commit()


def _signal(market_id: str = "m1", outcome: str = "YES") -> Signal:
    return Signal(
        id=f"sig-{market_id}",
        market_id=market_id,
        outcome=outcome,
        signal_type=SignalType.SMART_WALLET_ENTRY,
        score=85,
        confidence=0.85,
        reason="test",
        decision="PAPER_TRADE",
        proposed_size_usd=10.0,
        copyable_edge=0.04,
    )


def test_safe_paper_engine_refuses_candidate_elite_wallet() -> None:
    # v0.7.2: legacy named-mode test — single signal must clear the cluster
    # engine for the legacy validate_for_mode WALLET_NOT_RELIABLE path to
    # fire. Lower trigger_threshold to 0.25 (= one CANDIDATE_ELITE weight)
    # so the cluster passes through to the legacy gate.
    from app.services.paper_trading_engine import reset_cluster_engine
    from app.services.signal_cluster_engine import SignalClusterEngine
    reset_cluster_engine(SignalClusterEngine(trigger_threshold=0.25))
    engine = _engine()
    with Session(engine) as session:
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.commit()
        _seed_wallet(session, "0xcand", status="CANDIDATE_ELITE", sample=22)
        signal = _signal()
        session.add(signal)
        session.commit()
        result = PaperTradingEngine(session).maybe_auto_trade(
            signal,
            {
                "spread_pct": 0.01,
                "liquidity": 5_000,
                "copy_delay_seconds": 30,
                "price_deterioration": 0.0,
                "confidence_score": 80,
                "copyable_edge_score": 70,
                "wallet_tier": "STRONG",
                "orderbook_quality": "GOOD",
                "liquidity_score": 70,
                "price": 0.5,
                "side": "BUY",
                "wallet": "0xcand",
            },
            profile=SAFE,
        )
    assert result["executed"] is False
    assert result["reason_code"] == "WALLET_NOT_RELIABLE"
    assert result["profile"] == "SAFE"


def test_full_paper_engine_accepts_candidate_elite_wallet() -> None:
    # v0.7.2: lower cluster threshold so a single CANDIDATE_ELITE signal
    # passes the consensus gate and reaches the legacy validate_for_mode.
    from app.services.paper_trading_engine import reset_cluster_engine
    from app.services.signal_cluster_engine import SignalClusterEngine
    reset_cluster_engine(SignalClusterEngine(trigger_threshold=0.25))
    engine = _engine()
    with Session(engine) as session:
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.commit()
        set_active_mode(session, "FULL_PAPER")
        _seed_wallet(session, "0xcand", status="CANDIDATE_ELITE", sample=22)
        signal = _signal()
        session.add(signal)
        session.commit()
        result = PaperTradingEngine(session).maybe_auto_trade(
            signal,
            {
                "spread_pct": 0.01,
                "liquidity": 5_000,
                "copy_delay_seconds": 30,
                "price_deterioration": 0.0,
                "confidence_score": 80,
                "copyable_edge_score": 70,
                "wallet_tier": "STRONG",
                "orderbook_quality": "GOOD",
                "liquidity_score": 70,
                "price": 0.5,
                "side": "BUY",
                "wallet": "0xcand",
            },
        )
    # v0.7.2: BASE_PAPER preset (the only paper preset post-mode-removal)
    # restricts allowed_tiers to {ELITE, STRONG}. CANDIDATE_ELITE is no
    # longer auto-tradable in paper. The legacy FULL_PAPER profile that
    # allowed it was retired with the named-modes cleanup.
    assert result["executed"] is False
    assert result["reason_code"] == "WALLET_TIER"


def test_full_paper_blocked_when_total_exposure_already_at_cap() -> None:
    # v0.7.2: lower cluster threshold so a single STRONG signal clears
    # the cluster gate and reaches the legacy validate_for_mode TOO_MUCH_EXPOSURE.
    from app.services.paper_trading_engine import reset_cluster_engine
    from app.services.signal_cluster_engine import SignalClusterEngine
    reset_cluster_engine(SignalClusterEngine(trigger_threshold=0.25))
    engine = _engine()
    with Session(engine) as session:
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.commit()
        set_active_mode(session, "FULL_PAPER")
        _seed_wallet(session, "0xstrong", status="STRONG", sample=40)
        signal = _signal()
        session.add(signal)
        session.commit()

        # Build a massive context that pretends we already used 50% of paper capital.
        # FULL_PAPER caps total at 40%, so this must block with TOO_MUCH_EXPOSURE.
        ptengine = PaperTradingEngine(session)
        # Patch current_exposure for this single call.
        ptengine.current_exposure = lambda: 0.50  # type: ignore[assignment]
        result = ptengine.maybe_auto_trade(
            signal,
            {
                "spread_pct": 0.01,
                "liquidity": 5_000,
                "copy_delay_seconds": 30,
                "price_deterioration": 0.0,
                "confidence_score": 80,
                "copyable_edge_score": 70,
                "wallet_tier": "STRONG",
                "orderbook_quality": "GOOD",
                "liquidity_score": 70,
                "price": 0.5,
                "side": "BUY",
                "wallet": "0xstrong",
            },
        )
    assert result["executed"] is False
    assert result["reason_code"] == "TOO_MUCH_EXPOSURE"
    assert result["profile"] == "FULL_PAPER"
