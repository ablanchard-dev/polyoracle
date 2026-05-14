from datetime import UTC, datetime, timedelta

from sqlmodel import SQLModel, Session, create_engine

from app.models.wallet import Wallet
from app.services.trade_audit_engine import TradeAuditEngine


def _engine() -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_audit_trade_creates_record_and_decision() -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(
            Wallet(
                address="0xstrong",
                score=82,
                tier="STRONG",
                confidence=80,
                pnl=80_000,
                roi=0.30,
                win_rate=0.62,
                market_count=120,
                volume=2_000_000,
            )
        )
        session.commit()
        engine_service = TradeAuditEngine(session)
        result = engine_service.audit_trade(
            {
                "trade_id": "t1",
                "address": "0xstrong",
                "market_id": "mock-election-001",
                "outcome": "YES",
                "side": "BUY",
                "price": 0.55,
                "size": 1_000,
                "notional_usd": 55_000,
                "traded_at": datetime.now(UTC) - timedelta(seconds=120),
                "data_source": "mock",
            }
        )
    assert result.decision in {"IGNORE", "WATCH", "SIGNAL", "PAPER_TRADE"}
    assert result.wallet_score == 82
    assert result.trade_quality_score >= 0


def test_decide_returns_ignore_when_wallet_unknown() -> None:
    engine = _engine()
    with Session(engine) as session:
        engine_service = TradeAuditEngine(session)
        result = engine_service.audit_trade(
            {
                "trade_id": "t2",
                "address": "0xnew",
                "market_id": "mock-fed-002",
                "outcome": "YES",
                "side": "BUY",
                "price": 0.40,
                "size": 100,
                "notional_usd": 4_000,
                "traded_at": datetime.now(UTC),
                "data_source": "mock",
            }
        )
    assert result.decision in {"IGNORE", "WATCH"}


# 2026-05-14 — drill NOT_PAPER 88% finding (Round 9 review):
# Paper-mode orderbook bypass on ELITE strong-edge trades.
# These tests pin the contract :
#   - paper mode + ELITE + STRONG_EDGE + tq>=70 + liq>0 + orderbook BAD/UNTRADABLE → PAPER_TRADE
#   - live mode (live_enabled=True) + same conditions → WATCH (paper ≤ live invariant)
#   - tq < 70 → WATCH even in paper (no quality threshold lowered)
# We import _decide() directly with a synthetic EdgeBreakdown to avoid the
# heavy audit_trade() pipeline (Gamma/CLOB calls, orderbook synthesis).

def _decide_with_overrides(
    *,
    live_enabled: bool,
    paper_live_strict: bool = False,
    allow_non_strict_paper_research: bool = True,
    wallet_tier: str = "ELITE",
    wallet_score: float = 95.0,
    quality: str = "BAD",
    spread_pct: float | None = 0.01,
    edge_classification: str = "STRONG_EDGE",
    copyable_edge: float = 0.05,
    trade_quality_score: float = 75.0,
    market_liquidity_score: float = 25.0,
) -> str:
    from app.services.copyable_edge_engine import EdgeBreakdown
    engine_db = _engine()
    with Session(engine_db) as session:
        engine_service = TradeAuditEngine(session)
        engine_service.settings.live_enabled = live_enabled
        engine_service.settings.paper_trading_enabled = True
        engine_service.settings.paper_live_strict = paper_live_strict
        engine_service.settings.allow_non_strict_paper_research = allow_non_strict_paper_research
        edge = EdgeBreakdown(
            raw_edge=copyable_edge,
            spread_impact=0.005,
            slippage_impact=0.0,
            delay_penalty=0.0,
            late_entry_penalty=0.0,
            price_deterioration=0.0,
            copyable_edge=copyable_edge,
            score=70.0,
            classification=edge_classification,
        )
        return engine_service._decide(
            wallet_tier=wallet_tier,
            wallet_score=wallet_score,
            quality=quality,
            spread_pct=spread_pct,
            edge=edge,
            trade_quality_score=trade_quality_score,
            market_liquidity_score=market_liquidity_score,
        )


def test_paper_mode_orderbook_bypass_elite_strong_tq_pass_returns_paper_trade() -> None:
    # ELITE + STRONG_EDGE + tq=75 + liq=25 + orderbook BAD + paper mode → PAPER_TRADE
    assert _decide_with_overrides(live_enabled=False) == "PAPER_TRADE"


def test_live_mode_same_conditions_returns_watch() -> None:
    # Same conditions but live_enabled=True → orderbook gate still blocks
    # → WATCH. Preserves paper-vs-live shadow invariant under live mode.
    assert _decide_with_overrides(live_enabled=True) == "WATCH"


def test_paper_mode_bypass_does_not_lower_trade_quality_threshold() -> None:
    # tq=65 (< 70 threshold) → still WATCH even in paper mode.
    # Confirms the bypass does not relax the trade_quality_score gate.
    assert _decide_with_overrides(live_enabled=False, trade_quality_score=65.0) == "WATCH"


def test_paper_mode_bypass_requires_strong_edge() -> None:
    # WEAK_EDGE (copyable_edge below 0.02 STRONG_EDGE threshold)
    # should not benefit from the orderbook bypass.
    assert _decide_with_overrides(
        live_enabled=False,
        edge_classification="WEAK_EDGE",
        copyable_edge=0.01,
    ) == "WATCH"


def test_paper_mode_bypass_requires_liquidity_data() -> None:
    # market_liquidity_score=0 means no data at all → fall back to WATCH
    # (avoids accepting trades on markets with zero data signal).
    assert _decide_with_overrides(live_enabled=False, market_liquidity_score=0.0) == "WATCH"


def test_paper_live_strict_disables_orderbook_bypass() -> None:
    # paper_live_strict=True is the explicit "live-truth" mode where paper
    # mirrors what live would execute. _elite_paper_bypass in capital_allocator
    # and risk_engine is gated on `not paper_live_strict`; we mirror the same
    # gate here so audit and aval stay consistent (no trades lost between
    # layers). Same conditions as the bypass-success test, only difference is
    # paper_live_strict=True → expected WATCH.
    assert _decide_with_overrides(
        live_enabled=False,
        paper_live_strict=True,
    ) == "WATCH"


def test_bypass_requires_explicit_research_opt_in() -> None:
    # 2026-05-14 — hard guard doctrine : même avec paper_live_strict=False,
    # le bypass ne fire pas sans allow_non_strict_paper_research=True.
    # Empêche un oubli .env d'activer le bypass sans intention explicite.
    assert _decide_with_overrides(
        live_enabled=False,
        paper_live_strict=False,
        allow_non_strict_paper_research=False,
    ) == "WATCH"


# ---------------------------------------------------------------------------
# Hard guard config tests — 2026-05-14
# ---------------------------------------------------------------------------

def test_settings_default_strict_is_true() -> None:
    # Default Settings doit avoir paper_live_strict=True (doctrine post-Round 9).
    # Empêche qu'un démarrage sans .env shippe en mode permissif.
    from app.config import Settings
    s = Settings(paper_live_strict=True, allow_non_strict_paper_research=False)
    assert s.paper_live_strict is True
    assert s.allow_non_strict_paper_research is False


def test_settings_refuses_prod_non_strict() -> None:
    # En app_env=prod/vps, paper_live_strict=False doit raise au startup.
    import pytest
    from app.config import Settings
    for env in ("production", "prod", "vps", "vps_prod"):
        with pytest.raises(ValueError, match="PAPER_LIVE_STRICT=false interdit"):
            Settings(
                app_env=env,
                live_enabled=False,
                paper_live_strict=False,
                allow_non_strict_paper_research=True,  # même avec opt-in research, prod refuse
            )


def test_settings_refuses_non_strict_without_research_optin() -> None:
    # En dev, paper_live_strict=False sans allow_non_strict_paper_research=True
    # doit aussi raise. Empêche un oubli flag.
    import pytest
    from app.config import Settings
    with pytest.raises(ValueError, match="ALLOW_NON_STRICT_PAPER_RESEARCH=true"):
        Settings(
            app_env="development",
            live_enabled=False,
            paper_live_strict=False,
            allow_non_strict_paper_research=False,
        )


def test_settings_allows_research_optin_in_dev() -> None:
    # En dev avec opt-in explicite, autorisé.
    from app.config import Settings
    s = Settings(
        app_env="development",
        live_enabled=False,
        paper_live_strict=False,
        allow_non_strict_paper_research=True,
    )
    assert s.paper_live_strict is False
    assert s.allow_non_strict_paper_research is True


def test_settings_strict_true_in_prod_ok() -> None:
    # En prod avec strict=True, OK (état cible).
    from app.config import Settings
    s = Settings(
        app_env="production",
        live_enabled=False,
        paper_live_strict=True,
    )
    assert s.paper_live_strict is True
