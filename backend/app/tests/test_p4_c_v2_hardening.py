"""P4-C v2 hardening tests (review audit 2026-05-13).

Verifies that partial-scoring breakdowns are marked `decisional=False` —
they cannot be used to make promotion / lane decisions until enough
post-P0 data accumulates.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.wallet import MarketFirstWalletRecord
from app.services.copyable_wallet_score import (
    CopyableScoreBreakdown,
    compute_wallet_score,
)


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _make_mfwr(session, addr, wins=200, losses=10, recent_act=80):
    """Create an ELITE-tier MFWR."""
    mfwr = MarketFirstWalletRecord(
        address=addr,
        candidate_status="ELITE",
        resolved_winning_markets=wins,
        resolved_losing_markets=losses,
        resolved_market_win_rate=wins / (wins + losses),
        recent_activity_score=recent_act,
        composite_score=85.0,
    )
    session.add(mfwr)
    session.commit()
    return mfwr


def test_partial_scoring_is_NOT_decisional(session):
    """A wallet with only MFWR-based sub-scores (3/8) must be flagged
    decisional=False. The score is informational only."""
    _make_mfwr(session, "0xpartial", wins=200, losses=10)
    breakdown = compute_wallet_score(session, "0xpartial")
    assert isinstance(breakdown, CopyableScoreBreakdown)
    assert breakdown.score is not None  # score computed via re-normalization
    assert breakdown.decisional is False, (
        "Partial scoring must be flagged decisional=False — only 3/8 sub-scores "
        "available (no EntryPriceAudit / PublicTrade timestamps yet)."
    )
    assert breakdown.sub_scores_available < breakdown.sub_scores_required
    assert any("NOT DECISIONAL" in n for n in breakdown.notes)


def test_decisional_flag_default_threshold_is_6_of_8(session):
    """The required threshold should be 6/8 — i.e. ≥75% of sub-scores."""
    _make_mfwr(session, "0xthres", wins=200, losses=10)
    breakdown = compute_wallet_score(session, "0xthres")
    assert breakdown.sub_scores_required == 6


def test_no_mfwr_returns_no_score(session):
    """A wallet with no MFWR row returns score=None, decisional=False."""
    breakdown = compute_wallet_score(session, "0xnone")
    assert breakdown.score is None
    assert breakdown.decisional is False


def test_partial_score_can_be_high_but_must_not_be_decisional(session):
    """Even when partial score is excellent (>0.90), decisional must stay
    False until enough sub-scores are populated. This prevents premature
    'priority lane' assignment."""
    _make_mfwr(session, "0xhighpartial", wins=999, losses=1, recent_act=100)
    breakdown = compute_wallet_score(session, "0xhighpartial")
    # Score will be high (wr near 1.0, sample large, activity max)
    assert breakdown.score is not None and breakdown.score >= 0.90
    # But decisional must STILL be False (only 3/8 sub-scores)
    assert breakdown.decisional is False, (
        f"Score={breakdown.score:.4f} is high but decisional must remain False — "
        f"review Round 8: 'do not activate priority lane on partial scoring'."
    )
