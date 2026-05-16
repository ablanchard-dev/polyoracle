"""v0.7.8 P6 — polling cohort tier-aware + WR bucket filter tests.

12-tier refactor 2026-05-06 (operator spec):
- NANO/TINY/MICRO (<$500)  : ELITE GOLD only (wr ≥ 0.99)
- SMALL (<$1k)             : ELITE GOLD + SILVER (wr ≥ 0.95)
- MEDIUM-XXL ($1k-9.99k)   : ELITE GOLD+SILVER + STRONG GOLD overflow
- ELITE_OPEN (≥$10k)       : ELITE all (GOLD+SILVER+BRONZE) + STRONG GOLD overflow
- HUGE (≥$64k) / INST      : ELITE all only (preservation, no STRONG)

Priority within ELITE pool (computed-at-query):
- GOLD active (99-100% wr) first
- 95-99% active (SILVER)
- 90-95% active (BRONZE)
- semi/inactive ELITE in slow lane (NOT removed from cohort)

Tests verify:
1. NANO capital → cohort = ELITE GOLD only
2. ELITE_OPEN → cohort includes all WR buckets + priority order
3. MEDIUM → STRONG GOLD overflow allowed
4. Inactive ELITE in same bucket stays in cohort (slow lane)
5. STRONG SILVER/BRONZE never polled (overflow GOLD-only)
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
    # STRONG GOLD only — operator spec 2026-05-06: overflow GOLD-only.
    # STRONG SILVER/BRONZE never enter cohort regardless of capital.
    for i in range(strong_overflow):
        rows.append(MarketFirstWalletRecord(
            address=f"0xstrong_overflow_{i:04d}",
            candidate_status="STRONG",
            resolved_markets_traded=80,
            resolved_winning_markets=80,
            resolved_losing_markets=0,
            resolved_market_win_rate=0.995,  # GOLD bucket
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


def test_nano_capital_loads_elite_gold_and_silver_p6(tmp_path):
    """12-tier spec 2026-05-09: at NANO (<$200), cohort = ELITE GOLD + SILVER
    (was GOLD only pre-2026-05-09). BRONZE/STRONG/DROPPED still excluded.
    """
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=108.0,  # $108 = NANO tier
        )

        assert len(cohort) > 0
        # GOLD + SILVER (mid_*) addresses allowed. BRONZE (low_*, inactive_*),
        # STRONG and DROPPED still excluded.
        for addr in cohort:
            addr_lower = addr.lower()
            assert ("gold" in addr_lower or "mid" in addr_lower), (
                f"NANO cohort should only contain ELITE GOLD or SILVER, got {addr}"
            )
            assert "strong" not in addr_lower, f"STRONG in NANO cohort: {addr}"
            assert "low" not in addr_lower, f"BRONZE in NANO cohort: {addr}"
            assert "inactive" not in addr_lower, f"Inactive BRONZE in NANO cohort: {addr}"


def test_elite_open_polls_all_wr_buckets_in_priority_order_p6(tmp_path):
    """At ELITE_OPEN (≥$10k), all ELITE buckets allowed. Priority order:
    GOLD > SILVER > BRONZE, active > inactive (stale_penalty).
    """
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=15_000.0,  # ELITE_OPEN tier
        )

        # All ELITE buckets present
        gold_positions = [i for i, a in enumerate(cohort) if "gold" in a]
        mid_positions = [i for i, a in enumerate(cohort) if "mid" in a]
        low_positions = [i for i, a in enumerate(cohort) if "low" in a and "overflow" not in a]
        inactive_positions = [i for i, a in enumerate(cohort) if "inactive" in a]

        assert gold_positions, "no GOLD in cohort"
        assert mid_positions, "no SILVER (mid) in cohort"
        assert low_positions, "no BRONZE (low) in cohort"
        # GOLD active > SILVER active > BRONZE active (priority order)
        assert max(gold_positions) < min(mid_positions), (
            f"GOLD ranks {gold_positions} not all before mid {mid_positions}"
        )
        assert max(mid_positions) < min(low_positions), (
            f"mid {mid_positions} not all before low {low_positions}"
        )
        # Inactive ELITE (stale_penalty) ranks lowest
        if inactive_positions:
            assert min(inactive_positions) > max(low_positions), (
                f"inactive {inactive_positions} not at end after low {low_positions}"
            )


def test_inactive_elite_stays_in_cohort_at_compatible_tier_p6(tmp_path):
    """Inactive ELITE (recent_activity_score < 25) STAYS in cohort but ranks low.
    Tested at ELITE_OPEN where inactive's WR bucket (BRONZE 0.945) is allowed.
    """
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=15_000.0,  # ELITE_OPEN
        )

        inactive_in_cohort = [a for a in cohort if "inactive" in a]
        assert len(inactive_in_cohort) == 2, (
            "inactive ELITE in compatible bucket should stay in cohort (slow lane)"
        )


def test_strong_never_below_elite_open_p6(tmp_path):
    """12-tier spec 2026-05-09: STRONG never polled below $10k (ELITE_OPEN pivot).
    Includes NANO/TINY/MICRO/SMALL/MEDIUM/LARGE/XL/XXL — all ELITE-only."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        for cap_usd, tier_name in [
            (108.0, "NANO"),
            (220.0, "TINY"),
            (400.0, "MICRO"),
            (700.0, "SMALL"),
            (1500.0, "MEDIUM"),  # NEW spec: STRONG excluded
            (3000.0, "LARGE"),
            (5000.0, "XL"),
            (9000.0, "XXL"),
        ]:
            cohort = WalletPollingEngine.load_cohort(
                path=csv, session=session, capital_total=cap_usd,
            )
            strong_in_cohort = [a for a in cohort if "strong" in a]
            assert strong_in_cohort == [], (
                f"{tier_name} (${cap_usd}) cohort must exclude STRONG, got {strong_in_cohort}"
            )


def test_elite_open_capital_includes_strong_gold_only_p6(tmp_path):
    """12-tier spec 2026-05-09: at ELITE_OPEN (≥$10k pivot): STRONG GOLD overflow
    allowed (was MEDIUM ≥$1k pre-2026-05-09). STRONG SILVER/BRONZE never."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=15_000.0,  # ELITE_OPEN
        )
        strong_in_cohort = [a for a in cohort if "strong" in a]
        # Fixture seeded 2 STRONG GOLD (wr=0.995) → both should be present.
        assert len(strong_in_cohort) == 2, (
            f"ELITE_OPEN cohort should include STRONG GOLD overflow (2), got {strong_in_cohort}"
        )


def test_huge_capital_keeps_strong_gold_p6(tmp_path):
    """12-tier spec 2026-05-09: at HUGE (≥$64k): KEEPS STRONG GOLD overflow
    (was excluded 'preservation' pre-2026-05-09). HUGE/INST keep STRONG GOLD."""
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        addresses = [r.address for r in seeded]
        csv = _csv_path(tmp_path, addresses)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=80_000.0,  # HUGE
        )
        strong_in_cohort = [a for a in cohort if "strong" in a]
        # Fixture seeded 2 STRONG GOLD → kept at HUGE in new spec.
        assert len(strong_in_cohort) == 2, (
            f"HUGE cohort should keep STRONG GOLD overflow, got {strong_in_cohort}"
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


def test_mfwr_first_new_elite_not_in_csv_still_polled(tmp_path):
    """2026-05-16 bug fix : MFWR = source of truth runtime.

    Before this fix, load_cohort intersected MFWR ELITE with the legacy
    `validated_paper_universe_latest.csv`. Any wallet promoted to ELITE
    in MFWR but NOT in the stale CSV was invisible to the polling loop
    (= apply ELITE inutile pour runtime).

    After fix : if MFWR has rows, MFWR alone defines the cohort. CSV
    only used as fallback when MFWR empty.
    """
    eng = _engine()
    with Session(eng) as session:
        seeded = _seed_mfwr(session)
        # Build CSV WITHOUT the GOLD ELITE addresses (= simulate stale CSV
        # legacy v0.5.4 that doesn't include freshly promoted wallets).
        addresses_in_csv = [r.address for r in seeded if not r.address.startswith("0xgold_")]
        csv = _csv_path(tmp_path, addresses_in_csv)

        cohort = WalletPollingEngine.load_cohort(
            path=csv, session=session, capital_total=108.0,  # NANO tier
        )

        # GOLD wallets (0xgold_*) are MFWR ELITE GOLD but NOT in CSV.
        # With the fix, they must still be in cohort (MFWR = source of truth).
        new_elite_in_cohort = [a for a in cohort if a.startswith("0xgold_")]
        assert len(new_elite_in_cohort) > 0, (
            "MFWR ELITE GOLD wallets must be in cohort even if absent from CSV "
            "(MFWR = source of truth post-2026-05-16). "
            f"cohort={cohort} csv_addresses={addresses_in_csv[:3]}..."
        )
