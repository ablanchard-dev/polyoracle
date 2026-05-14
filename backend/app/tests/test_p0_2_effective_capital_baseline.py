"""P0.2 (Round 8 2026-05-12) — compute_effective_paper_capital baseline source.

In strict mode (`PAPER_LIVE_STRICT=true`) the capital calc MUST use
EFFECTIVE_BASELINE_T0 (the constant in baseline_constants.py), NOT the legacy
file `T0_paper_72h.txt` which may pre-date the strict cutover and silently
inflate the tier resolution.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.bot import BotState
from app.models.trade import PaperTrade
from app.services.baseline_constants import EFFECTIVE_BASELINE_T0
from app.services.paper_trading_engine import (
    _resolve_baseline_for_capital_calc,
    compute_effective_paper_capital,
)


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        # Seed BotState with paper_capital
        s.add(BotState(id=1, paper_capital=100.0))
        s.commit()
        yield s


# ---------------- _resolve_baseline_for_capital_calc ----------------


def test_strict_mode_returns_effective_baseline_t0_regardless_of_file():
    """Strict mode must NEVER use the legacy file T0."""
    with patch(
        "app.services.paper_trading_engine._read_paper_baseline_at",
        return_value=datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc),
    ):
        baseline, src = _resolve_baseline_for_capital_calc(strict_mode=True)
    assert baseline == EFFECTIVE_BASELINE_T0
    assert src == "effective_baseline_t0"


def test_non_strict_uses_file_baseline_when_present():
    """Non-strict legacy path: read from T0_paper_72h.txt."""
    file_t0 = datetime(2026, 5, 11, 14, 0, 0, tzinfo=timezone.utc)  # AFTER EFFECTIVE_T0
    with patch(
        "app.services.paper_trading_engine._read_paper_baseline_at",
        return_value=file_t0,
    ):
        baseline, src = _resolve_baseline_for_capital_calc(strict_mode=False)
    assert baseline == file_t0
    assert src == "file_t0"


def test_non_strict_no_file_returns_none():
    """No baseline anywhere — full DB."""
    with patch(
        "app.services.paper_trading_engine._read_paper_baseline_at",
        return_value=None,
    ):
        baseline, src = _resolve_baseline_for_capital_calc(strict_mode=False)
    assert baseline is None
    assert src == "none"


def test_non_strict_old_file_emits_warning(caplog):
    """File T0 older than EFFECTIVE_BASELINE_T0 should warn but still be used."""
    old_file_t0 = datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc)
    with patch(
        "app.services.paper_trading_engine._read_paper_baseline_at",
        return_value=old_file_t0,
    ):
        with caplog.at_level("WARNING"):
            baseline, src = _resolve_baseline_for_capital_calc(strict_mode=False)
    assert baseline == old_file_t0
    assert src == "file_t0"
    assert any("pre-date" in r.message for r in caplog.records)


# ---------------- compute_effective_paper_capital end-to-end ----------------


def _add_trade(session: Session, opened_at: datetime, pnl: float, status: str = "closed"):
    """Helper to add a paper trade with a specific opened_at + pnl."""
    session.add(PaperTrade(
        id=f"t-{opened_at.isoformat()}",
        market_id="0xm",
        outcome="Yes",
        side="BUY",
        quantity=1.0,
        average_price=0.5,
        realized_pnl=pnl,
        status=status,
        opened_at=opened_at,
    ))
    session.commit()


def test_strict_mode_filters_pre_effective_baseline_trades(session):
    """Strict mode: trades opened BEFORE EFFECTIVE_BASELINE_T0 must NOT be counted."""
    # 5 trades pre-baseline (10€ total PnL), 3 trades post-baseline (15€)
    for i in range(5):
        _add_trade(session, EFFECTIVE_BASELINE_T0 - timedelta(hours=i + 1), 2.0)
    for i in range(3):
        _add_trade(session, EFFECTIVE_BASELINE_T0 + timedelta(hours=i + 1), 5.0)

    with patch("app.services.paper_trading_engine.get_settings") as gs:
        gs.return_value.paper_live_strict = True
        capital = compute_effective_paper_capital(session)

    # base 100 + only post-baseline PnL 15 = 115
    assert capital == 115.0


def test_non_strict_mode_uses_file_baseline(session):
    """Non-strict: uses file T0 (patched via _resolve_baseline_for_capital_calc
    to avoid filesystem dependency).
    """
    file_t0 = EFFECTIVE_BASELINE_T0 + timedelta(days=1)
    _add_trade(session, file_t0 - timedelta(hours=12), 10.0)  # excluded
    _add_trade(session, file_t0 - timedelta(hours=6), 20.0)  # excluded
    _add_trade(session, file_t0 + timedelta(hours=6), 5.0)
    _add_trade(session, file_t0 + timedelta(hours=12), 7.0)
    _add_trade(session, file_t0 + timedelta(hours=18), 8.0)

    # Patch directly the resolver function — its behavior is unit-tested above.
    with patch("app.services.paper_trading_engine.get_settings") as gs, \
         patch(
             "app.services.paper_trading_engine._resolve_baseline_for_capital_calc",
             return_value=(file_t0, "file_t0"),
         ):
        gs.return_value.paper_live_strict = False
        capital = compute_effective_paper_capital(session)

    # base 100 + post-file_t0 PnL (5+7+8) = 120
    assert capital == 120.0


def test_no_botstate_returns_settings_fallback(session):
    """If BotState row absent, return the fallback value."""
    # Remove the seeded BotState
    state = session.get(BotState, 1)
    session.delete(state)
    session.commit()
    capital = compute_effective_paper_capital(session, settings_fallback=42.0)
    assert capital == 42.0


def test_strict_mode_with_no_trades_returns_base_capital(session):
    """No trades at all — strict mode returns paper_capital alone."""
    with patch("app.services.paper_trading_engine.get_settings") as gs:
        gs.return_value.paper_live_strict = True
        capital = compute_effective_paper_capital(session)
    assert capital == 100.0


def test_strict_mode_pre_p0_2_bug_prevented(session):
    """Regression: simulate the pre-P0.2 bug scenario.

    Pre-fix: file T0 = 2026-05-07 (old). Strict mode but the function used
    the file → counted 5 days of pre-strict trades in the tier capital.

    Post-fix: strict mode ignores the file, uses EFFECTIVE_BASELINE_T0
    (2026-05-11T13:21Z), so only post-fix trades count.
    """
    # Simulate: 10 trades on 2026-05-08 (way pre-baseline) with +50€ total
    pre_strict_dt = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        _add_trade(session, pre_strict_dt + timedelta(hours=i), 5.0)
    # And 4 trades post-baseline with +20€
    for i in range(4):
        _add_trade(session, EFFECTIVE_BASELINE_T0 + timedelta(hours=i + 1), 5.0)

    # Pre-P0.2 bug behavior: file T0 = 2026-05-07 → all 14 trades counted = 100 + 70 = 170
    # Post-P0.2 fix: strict mode → ignore file → only 4 trades post-baseline = 100 + 20 = 120
    old_file_t0 = datetime(2026, 5, 7, 0, 0, 0, tzinfo=timezone.utc)
    with patch("app.services.paper_trading_engine.get_settings") as gs, \
         patch("app.services.paper_trading_engine._read_paper_baseline_at",
               return_value=old_file_t0):
        gs.return_value.paper_live_strict = True
        capital = compute_effective_paper_capital(session)

    assert capital == 120.0, (
        f"P0.2 bug should be fixed: strict mode must ignore file T0. "
        f"Got {capital}, expected 120 (only 4 post-baseline trades counted)."
    )


# ---------------- STRICT_CUTOVER_AT override (2026-05-15 graduated capital) ----------------


def test_strict_cutover_at_override_wins_over_effective_baseline_t0():
    """When settings.strict_cutover_at is set + > EFFECTIVE_BASELINE_T0, use it.

    Use case opérateur 2026-05-15 : reset effective capital pour tester chaque
    tier (NANO→TINY→MICRO→SMALL...) en condition réelle.
    """
    later_cutover = EFFECTIVE_BASELINE_T0 + timedelta(days=4)
    with patch("app.services.paper_trading_engine.get_settings") as gs:
        gs.return_value.strict_cutover_at = later_cutover.isoformat()
        baseline, src = _resolve_baseline_for_capital_calc(strict_mode=True)
    assert baseline == later_cutover
    assert src == "strict_cutover_at_override"


def test_strict_cutover_at_override_ignored_if_older_than_t0():
    """Override ANTERIEUR à EFFECTIVE_BASELINE_T0 → ignoré, fallback T0."""
    older_cutover = EFFECTIVE_BASELINE_T0 - timedelta(days=1)
    with patch("app.services.paper_trading_engine.get_settings") as gs:
        gs.return_value.strict_cutover_at = older_cutover.isoformat()
        baseline, src = _resolve_baseline_for_capital_calc(strict_mode=True)
    # Doit retourner EFFECTIVE_BASELINE_T0, pas l'override
    assert baseline == EFFECTIVE_BASELINE_T0
    assert src == "effective_baseline_t0"


def test_strict_cutover_at_override_invalid_format_ignored():
    """ISO 8601 invalide → fallback graceful sur T0."""
    with patch("app.services.paper_trading_engine.get_settings") as gs:
        gs.return_value.strict_cutover_at = "not-a-date"
        baseline, src = _resolve_baseline_for_capital_calc(strict_mode=True)
    assert baseline == EFFECTIVE_BASELINE_T0
    assert src == "effective_baseline_t0"


def test_strict_cutover_at_override_none_fallback_t0():
    """settings.strict_cutover_at = None → comportement original."""
    with patch("app.services.paper_trading_engine.get_settings") as gs:
        gs.return_value.strict_cutover_at = None
        baseline, src = _resolve_baseline_for_capital_calc(strict_mode=True)
    assert baseline == EFFECTIVE_BASELINE_T0
    assert src == "effective_baseline_t0"


def test_strict_cutover_at_override_filters_trades_correctly(session):
    """End-to-end : override reset effective capital → seul PnL post-override compte."""
    cutover = EFFECTIVE_BASELINE_T0 + timedelta(days=3)
    # 5 trades entre T0 et cutover (= 50€ excluded post-reset)
    for i in range(5):
        _add_trade(session, EFFECTIVE_BASELINE_T0 + timedelta(hours=i + 1), 10.0)
    # 3 trades post-cutover (= 12€ included)
    for i in range(3):
        _add_trade(session, cutover + timedelta(hours=i + 1), 4.0)

    with patch("app.services.paper_trading_engine.get_settings") as gs:
        gs.return_value.paper_live_strict = True
        gs.return_value.strict_cutover_at = cutover.isoformat()
        capital = compute_effective_paper_capital(session)

    # base 100 + only post-cutover PnL 12 = 112
    assert capital == 112.0
