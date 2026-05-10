"""v0.7.8 P5-A — Capital Source-of-Truth coherence tests.

Why this exists: a real bug in production where Settings.paper_capital
defaulted to 1000 but BotState.paper_capital was set to 100. The engine
used Settings (1000), the operator saw BotState (100) in /bot/status,
and the allocator's tier resolution used the wrong value — admitting
STRONG wallets at what should have been an ELITE-only 100€ tier.

Contract enforced: PaperTradingEngine.paper_capital MUST equal
BotState.paper_capital, never Settings (unless BotState is missing).
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from app.models.bot import BotState
from app.services.paper_trading_engine import PaperTradingEngine


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


def test_engine_capital_matches_botstate_when_set():
    """When BotState.paper_capital is set, engine.paper_capital uses it,
    NOT settings.paper_capital."""
    eng = _engine()
    with Session(eng) as session:
        # Operator sets capital to 100€ via DB (what /bot/status reports)
        session.add(BotState(id=1, mode="PAPER", running=True, paper_capital=100.0))
        session.commit()

        engine = PaperTradingEngine(session)
        assert engine.paper_capital == 100.0, (
            f"engine.paper_capital={engine.paper_capital} should match BotState=100, "
            f"not settings default ({engine.settings.paper_capital})"
        )


def test_engine_capital_falls_back_to_settings_when_botstate_missing():
    """When BotState row absent (early boot/test), engine falls back to
    Settings. Backward-compat for existing test fixtures."""
    eng = _engine()
    with Session(eng) as session:
        # No BotState row
        engine = PaperTradingEngine(session)
        assert engine.paper_capital == float(engine.settings.paper_capital)


def test_engine_capital_falls_back_when_botstate_paper_capital_null():
    """When BotState exists but paper_capital is None, also fall back."""
    eng = _engine()
    with Session(eng) as session:
        # BotState with no paper_capital — falls back to Settings
        session.add(BotState(id=1, mode="PAPER", running=True))
        session.commit()
        # SQLModel default fills paper_capital=10_000 from the model. So
        # this scenario is identical to "BotState set". The test guards
        # against future schema drift where paper_capital becomes nullable.
        engine = PaperTradingEngine(session)
        # Either DB default or settings default, but always a valid number
        assert engine.paper_capital > 0


def test_engine_capital_used_in_allocator_evaluate():
    """End-to-end: when BotState.paper_capital=100, the engine passes
    capital_total=100 to the allocator (NOT settings.paper_capital).
    This is the bug that opened STRONG trades in smoke v3 — capital_total
    was 1000 which resolved to STANDARD (allows STRONG)."""
    eng = _engine()
    with Session(eng) as session:
        session.add(BotState(id=1, mode="PAPER", running=True, paper_capital=100.0))
        session.commit()

        engine = PaperTradingEngine(session)
        # v0.7.8 P6 — 12-tier refactor 2026-05-06: at $100 → NANO (ELITE GOLD only).
        assert engine.paper_capital == 100.0
        from app.services.capital_allocator import _resolve_tier
        tier = _resolve_tier(engine.paper_capital)
        assert tier["name"] == "NANO"
        assert tier["allowed_tiers_intersect"] == frozenset({"ELITE"})
        # New 12-tier spec: NANO → ELITE GOLD only, no STRONG.
        assert tier["allowed_elite_buckets"] == frozenset({"GOLD"})
        assert tier["allowed_strong_buckets"] == frozenset()


def test_capital_value_stable_within_engine_lifetime():
    """The engine reads capital ONCE at __init__. A subsequent BotState
    update mid-request should not change the engine's view (request-level
    consistency)."""
    eng = _engine()
    with Session(eng) as session:
        session.add(BotState(id=1, mode="PAPER", running=True, paper_capital=100.0))
        session.commit()

        engine = PaperTradingEngine(session)
        assert engine.paper_capital == 100.0

        # Mid-request: operator changes BotState
        state = session.get(BotState, 1)
        state.paper_capital = 5000.0
        session.add(state)
        session.commit()

        # Engine still sees 100 — its view is frozen at construction
        assert engine.paper_capital == 100.0, (
            "engine.paper_capital must be stable within a single request"
        )
