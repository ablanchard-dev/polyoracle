"""v0.7.8 Phase 5 — Weekly reclass service tests.

Critical invariants:
  1. Cohort never drops below COHORT_MIN_SIZE
  2. DB backup before any write
  3. Idempotent — running twice = same result
  4. Failure → rollback (no partial write)
  5. Promotion criteria match initial ELITE classification (no relax)
  6. Audit trail captures every promote/demote
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.wallet import MarketFirstWalletRecord
from app.services.weekly_reclass_service import (
    COHORT_MIN_SIZE,
    DEMOTE_THRESHOLD_RESOLVED,
    DEMOTE_THRESHOLD_WIN_RATE,
    ELITE_MIN_COMPOSITE_SCORE,
    ELITE_MIN_RESOLVED_MARKETS,
    ELITE_MIN_WIN_RATE,
    ReclassResult,
    _backup_db,
    _evaluate_for_demotion,
    _evaluate_for_promotion,
    _prune_old_backups,
    reclass_summary_md,
    run_weekly_reclass,
)


@pytest.fixture
def tmp_db_dir(tmp_path):
    return tmp_path


@pytest.fixture
def session_with_db(tmp_db_dir):
    db_path = tmp_db_dir / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session, db_path


def _seed_mfwr(session, *, n_elite=40, n_strong_promotable=10,
               n_elite_demoteable=2, n_dropped=5):
    """Seed MFWR rows with various profiles aligned with v0.7.8 P6 strict
    thresholds: ELITE = >=100 resolved & >=0.90 win_rate."""
    # Existing ELITE — solid stats (>=100 resolved, >=0.90 wr), should stay ELITE
    for i in range(n_elite):
        m = MarketFirstWalletRecord(
            address=f"0xelite_solid_{i:04d}",
            candidate_status="ELITE",
            composite_score=92.0,
            resolved_markets_traded=120,
            resolved_winning_markets=110,
            resolved_losing_markets=10,
            resolved_market_win_rate=0.916,  # 110/120
        )
        session.add(m)
    # STRONG that NOW meet new strict ELITE criteria → should be promoted
    for i in range(n_strong_promotable):
        m = MarketFirstWalletRecord(
            address=f"0xstrong_promo_{i:04d}",
            candidate_status="STRONG",
            composite_score=ELITE_MIN_COMPOSITE_SCORE + 1,
            resolved_markets_traded=ELITE_MIN_RESOLVED_MARKETS + 5,
            resolved_winning_markets=ELITE_MIN_RESOLVED_MARKETS,  # high enough
            resolved_losing_markets=5,
            resolved_market_win_rate=ELITE_MIN_WIN_RATE + 0.03,
        )
        session.add(m)
    # ELITE that lost edge → should be demoted (sample drops below DEMOTE)
    for i in range(n_elite_demoteable):
        m = MarketFirstWalletRecord(
            address=f"0xelite_demote_{i:04d}",
            candidate_status="ELITE",
            composite_score=70.0,
            resolved_markets_traded=DEMOTE_THRESHOLD_RESOLVED - 1,
            resolved_winning_markets=20,
            resolved_losing_markets=20,
            resolved_market_win_rate=0.50,  # well below DEMOTE_THRESHOLD_WIN_RATE
        )
        session.add(m)
    # DROPPED stay DROPPED
    for i in range(n_dropped):
        m = MarketFirstWalletRecord(
            address=f"0xdropped_{i:04d}",
            candidate_status="DROPPED",
            composite_score=30.0,
            resolved_markets_traded=5,
            resolved_winning_markets=1,
            resolved_losing_markets=4,
            resolved_market_win_rate=0.20,
        )
        session.add(m)
    session.commit()


# ---------------- 1 — promotion logic ----------------


def test_promote_strong_meeting_elite_criteria():
    class _Row:
        address = "0xtest"
        composite_score = ELITE_MIN_COMPOSITE_SCORE + 1
        resolved_markets_traded = ELITE_MIN_RESOLVED_MARKETS + 5
        resolved_market_win_rate = ELITE_MIN_WIN_RATE + 0.05

    decision = _evaluate_for_promotion(_Row(), "STRONG")
    assert decision is not None
    assert decision.new_status == "ELITE"
    assert decision.previous_status == "STRONG"


def test_no_promote_strong_below_composite_threshold():
    class _Row:
        address = "0xtest"
        composite_score = ELITE_MIN_COMPOSITE_SCORE - 5  # too low
        resolved_markets_traded = 100  # high
        resolved_market_win_rate = 0.80  # high

    assert _evaluate_for_promotion(_Row(), "STRONG") is None


def test_no_promote_strong_with_low_sample():
    class _Row:
        address = "0xtest"
        composite_score = 99
        resolved_markets_traded = ELITE_MIN_RESOLVED_MARKETS - 5  # too few
        resolved_market_win_rate = 0.99

    assert _evaluate_for_promotion(_Row(), "STRONG") is None


def test_no_promote_already_elite():
    class _Row:
        address = "0xtest"
        composite_score = 99
        resolved_markets_traded = 100
        resolved_market_win_rate = 0.80

    assert _evaluate_for_promotion(_Row(), "ELITE") is None


# ---------------- 2 — demotion logic ----------------


def test_demote_elite_with_low_sample():
    """An ELITE wallet whose resolved sample drops below the demote
    threshold should be demoted. v0.7.8 P6 demote rule: resolved < 90
    OR win_rate < 0.85 (5pp/10pt buffer below ELITE promotion bar)."""
    class _Row:
        address = "0xtest"
        composite_score = 90
        resolved_markets_traded = DEMOTE_THRESHOLD_RESOLVED - 1  # below floor
        resolved_winning_markets = 50
        resolved_losing_markets = 30
        resolved_market_win_rate = 0.55  # also below DEMOTE_THRESHOLD_WIN_RATE

    decision = _evaluate_for_demotion(_Row(), "ELITE")
    assert decision is not None
    # Where it lands depends on whether it still meets STRONG: with resolved<70
    # and wr<0.70 it drops to DROPPED. Either way, NOT ELITE.
    assert decision.new_status in ("STRONG", "DROPPED")


def test_no_demote_elite_with_solid_stats():
    """ELITE with >=100 resolved AND >=0.90 wr stays ELITE (strict new spec)."""
    class _Row:
        address = "0xtest"
        composite_score = 92
        resolved_markets_traded = 120
        resolved_winning_markets = 110
        resolved_losing_markets = 10
        resolved_market_win_rate = 0.916

    assert _evaluate_for_demotion(_Row(), "ELITE") is None


def test_no_demote_already_strong():
    """Demote rule for STRONG only fires if STRONG drops below STRONG bar
    (resolved<60 or wr<0.65). A STRONG with resolved=80, wr=0.75 stays."""
    class _Row:
        address = "0xtest"
        composite_score = 50
        resolved_markets_traded = 80
        resolved_winning_markets = 60
        resolved_losing_markets = 20
        resolved_market_win_rate = 0.75

    assert _evaluate_for_demotion(_Row(), "STRONG") is None


# ---------------- 3 — DB backup ----------------


def test_db_backup_creates_file(tmp_db_dir):
    db_path = tmp_db_dir / "src.db"
    db_path.write_text("fake db content")
    backup_dir = tmp_db_dir / "backups"

    backup_path = _backup_db(db_path, backup_dir)
    assert backup_path.exists()
    assert backup_path.read_text() == "fake db content"
    assert "polyoracle_pre_reclass_" in backup_path.name


def test_db_backup_raises_if_db_missing(tmp_db_dir):
    db_path = tmp_db_dir / "nonexistent.db"
    with pytest.raises(FileNotFoundError):
        _backup_db(db_path, tmp_db_dir / "backups")


# ---------------- 4 — full reclass dry run ----------------


def test_reclass_dry_run_does_not_modify_db(session_with_db):
    session, db_path = session_with_db
    _seed_mfwr(session)

    # Count ELITE before
    before = len(session.exec(
        SQLModel.metadata.tables["marketfirstwalletrecord"].select()
        .where(SQLModel.metadata.tables["marketfirstwalletrecord"].c.candidate_status == "ELITE")
    ).all())

    result = run_weekly_reclass(
        session,
        db_path=db_path,
        backup_dir=db_path.parent / "backups",
        dry_run=True,
    )
    # Decisions computed but not applied
    assert len(result.promoted) >= 1
    assert len(result.demoted) >= 1

    after = len(session.exec(
        SQLModel.metadata.tables["marketfirstwalletrecord"].select()
        .where(SQLModel.metadata.tables["marketfirstwalletrecord"].c.candidate_status == "ELITE")
    ).all())
    assert before == after, "Dry run should not modify DB"


# ---------------- 5 — full reclass real run ----------------


def test_reclass_promotes_and_demotes(session_with_db):
    session, db_path = session_with_db
    _seed_mfwr(
        session, n_elite=40, n_strong_promotable=10,
        n_elite_demoteable=2, n_dropped=5,
    )

    result = run_weekly_reclass(
        session, db_path=db_path,
        backup_dir=db_path.parent / "backups",
    )

    # v0.7.8 P6 strict thresholds (>=100 resolved & >=0.90 wr for ELITE):
    # - 40 solid ELITE seeded (resolved=120, wr=0.916) → stay ELITE
    # - 10 STRONG promotable (resolved=105, wr=0.93) → promoted to ELITE
    # - 2 ELITE demoteable (resolved=89, wr=0.50) → demoted (below ELITE bar)
    # - 5 DROPPED → stay DROPPED
    assert result.rolled_back is False
    assert len(result.promoted) == 10  # all 10 STRONG promote
    assert len(result.demoted) == 2    # 2 ELITE demote
    # Final ELITE cohort = 40 (solid) + 10 (promoted) = 50
    assert result.cohort_after == 50
    assert result.db_backup_path is not None


# ---------------- 6 — cohort safety: never below min ----------------


def test_reclass_rolls_back_if_cohort_drops_too_low(session_with_db):
    session, db_path = session_with_db
    # Seed only a few ELITE that all need demotion → cohort would drop to 0
    for i in range(5):
        m = MarketFirstWalletRecord(
            address=f"0xelite_bad_{i}",
            candidate_status="ELITE",
            composite_score=10,  # awful
            resolved_markets_traded=5,
            resolved_market_win_rate=0.20,
        )
        session.add(m)
    session.commit()

    result = run_weekly_reclass(
        session, db_path=db_path,
        backup_dir=db_path.parent / "backups",
    )

    assert result.rolled_back is True
    assert any("COHORT_SAFETY" in e for e in result.errors)
    # ELITE cohort unchanged
    elite_count = len([
        r for r in session.exec(
            SQLModel.metadata.tables["marketfirstwalletrecord"].select()
        ).all()
        if r.candidate_status == "ELITE"
    ])
    assert elite_count == 5  # nothing demoted


# ---------------- 7 — idempotent ----------------


def test_reclass_idempotent_two_runs_same_result(session_with_db):
    session, db_path = session_with_db
    _seed_mfwr(session)

    r1 = run_weekly_reclass(
        session, db_path=db_path,
        backup_dir=db_path.parent / "backups",
    )

    r2 = run_weekly_reclass(
        session, db_path=db_path,
        backup_dir=db_path.parent / "backups",
    )

    # Second run: nothing to do (all already classified correctly)
    assert len(r2.promoted) == 0
    assert len(r2.demoted) == 0
    assert r2.cohort_after == r1.cohort_after


# ---------------- 8 — backup pruning ----------------


def test_prune_old_backups(tmp_db_dir):
    backup_dir = tmp_db_dir / "backups"
    backup_dir.mkdir()
    # Create 3 backup files
    (backup_dir / "polyoracle_pre_reclass_20250101T000000Z.db").write_text("old")
    (backup_dir / "polyoracle_pre_reclass_20260101T000000Z.db").write_text("old2")
    new_file = backup_dir / "polyoracle_pre_reclass_2026now.db"
    new_file.write_text("new")
    # Force old mtime on the first 2
    import os
    import time
    old_time = time.time() - (60 * 60 * 24 * 60)  # 60 days ago
    for f in backup_dir.glob("polyoracle_pre_reclass_2025*.db"):
        os.utime(f, (old_time, old_time))
    for f in backup_dir.glob("polyoracle_pre_reclass_20260101*.db"):
        os.utime(f, (old_time, old_time))

    deleted = _prune_old_backups(backup_dir, retention_days=30)
    assert deleted == 2  # old ones removed
    assert new_file.exists()  # new one kept


# ---------------- 9 — summary report ----------------


def test_reclass_summary_md_includes_metrics():
    result = ReclassResult(
        started_at="2026-05-04T20:00Z",
        finished_at="2026-05-04T20:05Z",
        cohort_before=59,
        cohort_after=68,
    )
    md = reclass_summary_md(result)
    assert "59" in md
    assert "68" in md
    assert "Cohort" in md
