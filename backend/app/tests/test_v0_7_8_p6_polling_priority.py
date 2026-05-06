"""v0.7.8 P6 — polling cohort tier-aware + priority ORDER BY tests.

Operator rule (2026-05-05):
- SMALL (<$500) : ELITE only — no STRONG in tradable polling
- MEDIUM/LARGE  : ELITE + STRONG (STRONG_OVERFLOW gate post-smoke)
- HUGE (≥$50k)  : ELITE only (institutional)

Priority within ELITE pool (computed-at-query):
- GOLD active (99-100% wr) first
- 95-99% active
- 90-95% active
- semi/inactive ELITE in slow lane (NOT removed from cohort)

Tests verify:
1. SMALL capital → cohort excludes STRONG
2. MEDIUM/LARGE → cohort includes STRONG
3. GOLD active polled before lower-WR ELITE
4. Inactive ELITE stays in cohort but ranked low
5. Promotion candidates (STRONG ≥70 W+L, ≥0.90 wr) NOT in SMALL polling
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.models.wallet import MarketFirstWalletRecord
from app.services.wallet_polling_engine import WalletPollingEngine


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


def _csv_path(tmp_path: Path, addresses: list[str]) -> Path:
    """Build a minimal validated_paper_universe csv with allowed_aggressive=true."""
    p = tmp_path / "universe.csv"
    lines = ["address,allowed_aggressive"]
    for a in addresses:
        lines.append(f"{a},true")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _seed_mfwr(session: Session, *, gold_active=3, mid_active=3, low_active=3,
               inactive_elite=2, strong_overflow=2, dropped=2):
    """Seed MFWR with various profiles for priority tests."""
    rows = []
    # GOLD active (99-100% wr, recent_activity_score >= 70)
    for i in range(gold_active):
        rows.append(MarketFirstWalletRecord(
            address=f"0xgold_{i:04d}",
            candidate_status="ELITE",
            resolved_markets_traded=200,
            resolved_winning_markets=199,
            resolved_losing_markets=1,
            resolved_market_win_rate=0.995,
            recent_activity_score=85.0,
        ))
    # 95-99% active
    for i in range(mid_active):
        rows.append(MarketFirstWalletRecord(
            address=f"0xmid_{i:04d}",
            candidate_status="ELITE",
            resolved_markets_traded=150,
            resolved_winning_markets=140,
            resolved_losing_markets=10,
            resolved_market_win_rate=0.96,
            recent_activity_score=80.0,
        ))
    # 90-95% active
    for i in range(low_active):
        rows.append(MarketFirstWalletRecord(
            address=f"0xlow_{i:04d}",
            candidate_status="ELITE",
            resolved_markets_traded=120,
            resolved_winning_markets=110,
            resolved_losing_markets=10,
            resolved_market_win_rate=0.92,
            recent_activity_score=72.0,
        ))
    # ELITE inactive (still in cohort but should rank LOW)
    for i in range(inactive_elite):
        rows.append(MarketFirstWalletRecord(
            address=f"0xinactive_{i:04d}",
            candidate_status="ELITE",
            resolved_markets_traded=180,
            resolved_winning_markets=170,
            resolved_losing_markets=10,
            resolved_market_win_rate=0.945,
            recent_activity_score=10.0,  # very inactive
        ))
    # STRONG that meet overflow criteria (≥70 W+L, ≥0.90 wr) — should NOT be in SMALL cohort
    for i in range(strong_overflow):
        rows.append(MarketFirstWalletRecord(
            address=f"0xstrong_overflow_{i:04d}",
            candidate_status="STRONG",
            resolved_markets_traded=80,
            resolved_winning_markets=75,
            resolved_losing_markets=5,
            resolved_market_win_rate=0.937,
            recent_activity_score=80.0,
        ))
    # DROPPED — never in cohort
    for i in range(dropped):
        rows.append(MarketFirstWalletRecord(
            address=f"0xdropped_{i:04d}",
            candidate_status="DROPPED",
            resolved_markets_traded=10,
            resolved_winning_markets=2,
            resolved_losing_markets=8,
            resolved_market_win_rate=0.20,
            recent_activity_score=20.0,
        ))
    for r in rows:
        session.add(r)
    session.commit()
    return rows


def test_small_capital_loads_only_elite_p6(tmp_path):
    """At SMALL (<$500), cohort must exclude STRONG. Operator rule."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv,
            session=session,
            capital_total=108.0,  # $108 = SMALL tier
        )

        assert len(cohort) > 0
        # No STRONG / DROPPED in cohort
        for addr in cohort:
            assert "strong" not in addr.lower()
            assert "dropped" not in addr.lower()
        # All ELITE (incl inactive) should be present
        elite_count = sum(1 for a in cohort if "elite" in a.lower() or "gold" in a.lower() or "mid" in a.lower() or "low" in a.lower() or "inactive" in a.lower())
        assert elite_count == 11  # 3 GOLD + 3 mid + 3 low + 2 inactive = 11


def test_gold_active_polled_before_lower_wr_p6(tmp_path):
    """GOLD active (99-100% wr, active) must be polled BEFORE 95-99% / 90-95%."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=108.0,
        )

        # Find positions of GOLD vs lower-wr buckets
        gold_positions = [i for i, a in enumerate(cohort) if "gold" in a]
        mid_positions = [i for i, a in enumerate(cohort) if "mid" in a]
        low_positions = [i for i, a in enumerate(cohort) if "low" in a and "overflow" not in a]
        inactive_positions = [i for i, a in enumerate(cohort) if "inactive" in a]

        assert gold_positions, "no GOLD in cohort"
        assert mid_positions, "no mid in cohort"
        assert low_positions, "no low in cohort"
        # GOLD positions must be all BEFORE any mid/low
        assert max(gold_positions) < min(mid_positions), (
            f"GOLD ranks {gold_positions} not all before mid {mid_positions}"
        )
        assert max(mid_positions) < min(low_positions), (
            f"mid {mid_positions} not all before low {low_positions}"
        )
        # Inactive ELITE should be at the end (lowest priority due to stale_penalty)
        if inactive_positions:
            assert min(inactive_positions) > max(low_positions), (
                f"inactive {inactive_positions} not at end after low {low_positions}"
            )


def test_inactive_elite_in_cohort_but_low_priority_p6(tmp_path):
    """Inactive ELITE (recent_activity_score < 25) STAYS in cohort but ranks low.
    Operator rule: don't filter out, just deprioritize."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=108.0,
        )

        inactive_in_cohort = [a for a in cohort if "inactive" in a]
        assert len(inactive_in_cohort) == 2, (
            "inactive ELITE should be in cohort (slow lane), not removed"
        )


def test_strong_never_in_small_capital_polling_p6(tmp_path):
    """STRONG with overflow-eligible criteria (≥70 W+L, ≥0.90 wr) must NOT
    be in SMALL cohort. They're reclass/promotion candidates only at <$500."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=108.0,
        )
        strong_in_cohort = [a for a in cohort if "strong" in a]
        assert strong_in_cohort == [], (
            f"STRONG must not be in SMALL cohort, got {strong_in_cohort}"
        )


def test_medium_capital_includes_strong_p6(tmp_path):
    """At MEDIUM (≥$500): cohort INCLUDES STRONG (per locked spec)."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=1080.0,  # $1080 = MEDIUM
        )
        strong_in_cohort = [a for a in cohort if "strong" in a]
        assert len(strong_in_cohort) == 2, (
            f"MEDIUM cohort should include STRONG, got {strong_in_cohort}"
        )


def test_huge_capital_excludes_strong_p6(tmp_path):
    """At HUGE (≥$50k): ELITE only (institutional). STRONG excluded."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=60_000.0,  # HUGE
        )
        strong_in_cohort = [a for a in cohort if "strong" in a]
        assert strong_in_cohort == [], (
            f"HUGE cohort should exclude STRONG (institutional), got {strong_in_cohort}"
        )


def test_no_capital_param_back_compat(tmp_path):
    """When capital_total is None, behave like before (ELITE+STRONG)."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=None,
        )
        # Both ELITE and STRONG present
        elite_count = sum(1 for a in cohort if "elite" in a or "gold" in a or "mid" in a or "low" in a or "inactive" in a)
        strong_count = sum(1 for a in cohort if "strong" in a)
        assert elite_count > 0
        assert strong_count > 0
