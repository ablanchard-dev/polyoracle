"""v0.7.6 B13 — capital-aware duration filter tests."""

from __future__ import annotations

from app.services.duration_filter import (
    DURATION_BUCKETS,
    classify_duration,
    compute_capital_lock_penalty,
    compute_capital_turnover_score,
    compute_ev_per_hour,
    filter_duration_for_capital,
)


def test_50eur_rejects_long_normal_ev():
    """50€ + LONG bucket + normal EV → CAPITAL_LOCK_TOO_LONG (Tier 1)."""
    allowed, reason = filter_duration_for_capital(
        capital_total=50.0,
        duration_bucket="LONG",
        ev_lower_bound=0.05,
        capital_turnover_score=15.0,
    )
    assert allowed is False
    assert reason == "CAPITAL_LOCK_TOO_LONG"


def test_50eur_rejects_long_even_with_exceptional_ev():
    """v0.7.8 — 50€ + LONG always rejected. Override only applies to SHORT
    in Tier 1, never to LONG/VERY_LONG. Operator rule: small capital must
    rotate fast (1MN/5MN/10MN), no exception."""
    allowed, reason = filter_duration_for_capital(
        capital_total=50.0,
        duration_bucket="LONG",
        ev_lower_bound=0.25,
        capital_turnover_score=35.0,
    )
    assert allowed is False
    assert reason == "CAPITAL_LOCK_TOO_LONG"


def test_50eur_short_only_ultra_short_auto_very_short_conditional():
    """v0.7.8 P6 — at SMALL (<$500), short-only:
    - ULTRA_SHORT (0-5 min) auto-allow
    - VERY_SHORT (5-10 min, requires expected_minutes) edge-conditional
    - VERY_SHORT (10-30 min) reject
    - SHORT+ reject"""
    # ULTRA_SHORT auto-allow
    allowed, _ = filter_duration_for_capital(
        capital_total=50.0, duration_bucket="ULTRA_SHORT",
    )
    assert allowed is True

    # VERY_SHORT without minutes → conservative reject (we can't tell ≤10 vs >10)
    allowed_no_min, _ = filter_duration_for_capital(
        capital_total=50.0, duration_bucket="VERY_SHORT",
    )
    assert allowed_no_min is False

    # VERY_SHORT 8min with edge → allow
    allowed_8min_edge, _ = filter_duration_for_capital(
        capital_total=50.0, duration_bucket="VERY_SHORT",
        expected_minutes=8.0, ev_lower_bound=0.10, capital_turnover_score=20.0,
    )
    assert allowed_8min_edge is True

    # VERY_SHORT 8min without edge → reject
    allowed_8min_noedge, _ = filter_duration_for_capital(
        capital_total=50.0, duration_bucket="VERY_SHORT",
        expected_minutes=8.0, ev_lower_bound=0.02, capital_turnover_score=10.0,
    )
    assert allowed_8min_noedge is False

    # VERY_SHORT 20min → reject (>10 at SMALL, no exception)
    allowed_20min, _ = filter_duration_for_capital(
        capital_total=50.0, duration_bucket="VERY_SHORT",
        expected_minutes=20.0, ev_lower_bound=0.50, capital_turnover_score=80.0,
    )
    assert allowed_20min is False

    # SHORT and beyond → reject at SMALL (no exception in P6)
    for bucket in ("SHORT", "MEDIUM", "LONG", "VERY_LONG"):
        allowed_b, _ = filter_duration_for_capital(
            capital_total=50.0, duration_bucket=bucket,
            ev_lower_bound=0.50, capital_turnover_score=80.0,
        )
        assert allowed_b is False, f"SMALL must reject {bucket} no exception"


def test_500eur_medium_tier_short_mostly():
    """v0.7.8 P6 — MEDIUM ($500-5000): 1-30 auto, 30-120 edge, 120-1440 strong, >24h reject."""
    # ULTRA_SHORT, VERY_SHORT auto-allow
    for bucket in ("ULTRA_SHORT", "VERY_SHORT"):
        allowed, _ = filter_duration_for_capital(
            capital_total=540.0, duration_bucket=bucket,
        )
        assert allowed is True
    # SHORT (30-120) edge-conditional with min_ev=0.10/turn=20
    allowed_short, _ = filter_duration_for_capital(
        capital_total=540.0, duration_bucket="SHORT",
        ev_lower_bound=0.10, capital_turnover_score=20.0,
    )
    assert allowed_short is True
    # MEDIUM (120-1440) strong exception only (ev≥0.20)
    allowed_med_low, _ = filter_duration_for_capital(
        capital_total=540.0, duration_bucket="MEDIUM",
        ev_lower_bound=0.10, capital_turnover_score=20.0,
    )
    assert allowed_med_low is False
    allowed_med_high, _ = filter_duration_for_capital(
        capital_total=540.0, duration_bucket="MEDIUM",
        ev_lower_bound=0.25, capital_turnover_score=35.0,
    )
    assert allowed_med_high is True
    # LONG, VERY_LONG → reject at MEDIUM
    for bucket in ("LONG", "VERY_LONG"):
        allowed_b, _ = filter_duration_for_capital(
            capital_total=540.0, duration_bucket=bucket,
        )
        assert allowed_b is False


def test_5000eur_large_tier_caps_at_24h():
    """Operator rule 2026-05-16 : avant $50k, aucun trade >24h.
    LARGE ($5k-50k) : ULTRA_SHORT..MEDIUM auto, LONG/VERY_LONG HARD reject
    (pas d'edge override pour long duration)."""
    for bucket in ("ULTRA_SHORT", "VERY_SHORT", "SHORT", "MEDIUM"):
        allowed, _ = filter_duration_for_capital(
            capital_total=5400.0, duration_bucket=bucket,
        )
        assert allowed is True, f"LARGE should accept {bucket} auto"
    # LONG : hard reject même avec edge fort
    rej_long, reason = filter_duration_for_capital(
        capital_total=5400.0, duration_bucket="LONG",
        ev_lower_bound=0.30, capital_turnover_score=50.0,
    )
    assert rej_long is False
    assert reason == "CAPITAL_LOCK_TOO_LONG"
    # VERY_LONG hard reject
    rej_vl, _ = filter_duration_for_capital(
        capital_total=5400.0, duration_bucket="VERY_LONG",
    )
    assert rej_vl is False


def test_operator_rule_no_long_below_50k():
    """Operator rule 2026-05-16 cascade : LONG/VERY_LONG hard reject below $50k
    quel que soit l'edge. Tests capital 100, 1000, 10000, 49000."""
    for cap in (100.0, 1000.0, 10000.0, 49000.0):
        for bucket in ("LONG", "VERY_LONG"):
            # Even with extreme edge, no allow
            rej, reason = filter_duration_for_capital(
                capital_total=cap, duration_bucket=bucket,
                ev_lower_bound=0.50, capital_turnover_score=80.0,
            )
            assert rej is False, f"cap={cap} bucket={bucket} should reject"
            assert reason == "CAPITAL_LOCK_TOO_LONG"


def test_50000eur_huge_tier_institutional():
    """v0.7.8 P6 — HUGE (≥$50k): all buckets auto except VERY_LONG which
    needs strong EV+turnover."""
    for bucket in ("ULTRA_SHORT", "VERY_SHORT", "SHORT", "MEDIUM", "LONG"):
        allowed, _ = filter_duration_for_capital(
            capital_total=54000.0, duration_bucket=bucket,
        )
        assert allowed is True, f"HUGE should accept {bucket} auto"
    # VERY_LONG with edge → allow
    allowed_vl, _ = filter_duration_for_capital(
        capital_total=54000.0, duration_bucket="VERY_LONG",
        ev_lower_bound=0.15, capital_turnover_score=35.0,
    )
    assert allowed_vl is True
    # VERY_LONG no edge → reject
    rej_vl, _ = filter_duration_for_capital(
        capital_total=54000.0, duration_bucket="VERY_LONG",
    )
    assert rej_vl is False


# ---------------- v0.7.8 — 8 ciblés tests per operator spec ----------------


def test_b13_100eur_rejects_LONG_bucket():
    """Le bug central : à 100€, un trade LONG (1-7 jours) doit être rejeté."""
    allowed, reason = filter_duration_for_capital(
        capital_total=108.0, duration_bucket="LONG",
        ev_lower_bound=0.50, capital_turnover_score=80.0,
    )
    assert allowed is False
    assert reason == "CAPITAL_LOCK_TOO_LONG"


def test_b13_100eur_rejects_VERY_LONG_bucket():
    allowed, reason = filter_duration_for_capital(
        capital_total=108.0, duration_bucket="VERY_LONG",
        ev_lower_bound=0.50, capital_turnover_score=80.0,
    )
    assert allowed is False
    assert reason == "CAPITAL_LOCK_TOO_LONG"


def test_b13_100eur_rejects_MEDIUM_or_requires_exception():
    """À 100€, MEDIUM doit être rejeté par défaut (pas d'override en Tier 1)."""
    # Default: reject
    allowed, reason = filter_duration_for_capital(
        capital_total=108.0, duration_bucket="MEDIUM",
        ev_lower_bound=0.10, capital_turnover_score=20.0,
    )
    assert allowed is False
    assert reason == "CAPITAL_LOCK_TOO_LONG"
    # Even with high EV: still reject (Tier 1 has no MEDIUM override)
    allowed_high, reason_high = filter_duration_for_capital(
        capital_total=108.0, duration_bucket="MEDIUM",
        ev_lower_bound=0.50, capital_turnover_score=80.0,
    )
    assert allowed_high is False, "Tier 1 MUST reject MEDIUM even with high EV"


def test_b13_100eur_allows_ULTRA_SHORT():
    allowed, reason = filter_duration_for_capital(
        capital_total=108.0, duration_bucket="ULTRA_SHORT",
    )
    assert allowed is True
    assert reason is None


def test_b13_100eur_allows_VERY_SHORT_with_minutes():
    """v0.7.8 P6 — at SMALL, VERY_SHORT requires expected_minutes parameter
    AND minutes ≤ 10 AND edge condition met. Without minutes → conservative reject."""
    # No minutes → reject (conservative)
    allowed_no_min, _ = filter_duration_for_capital(
        capital_total=108.0, duration_bucket="VERY_SHORT",
    )
    assert allowed_no_min is False
    # 8 min with edge → allow
    allowed, _ = filter_duration_for_capital(
        capital_total=108.0, duration_bucket="VERY_SHORT",
        expected_minutes=8.0, ev_lower_bound=0.10, capital_turnover_score=20.0,
    )
    assert allowed is True


def test_b13_100eur_SHORT_always_rejects_v0_7_8_p6():
    """v0.7.8 P6 — at SMALL (<$500), SHORT (30-120 min) is ALWAYS rejected.
    Operator rule: small capital must finish trades fast. No exception."""
    for ev, turn in [(0.10, 20.0), (0.30, 15.0), (0.10, 50.0), (0.25, 35.0)]:
        allowed, _ = filter_duration_for_capital(
            capital_total=108.0, duration_bucket="SHORT",
            ev_lower_bound=ev, capital_turnover_score=turn,
        )
        assert allowed is False, f"SMALL must reject SHORT (ev={ev}, turn={turn})"


def test_b13_500eur_medium_tier_policy_v0_7_8_p6():
    """v0.7.8 P6 — at MEDIUM ($500-5000): SHORT edge-conditional (ev≥0.10),
    MEDIUM strong-exception (ev≥0.20), LONG/VERY_LONG always reject."""
    # ULTRA_SHORT/VERY_SHORT auto-allow
    for bucket in ("ULTRA_SHORT", "VERY_SHORT"):
        allowed, _ = filter_duration_for_capital(
            capital_total=540.0, duration_bucket=bucket,
        )
        assert allowed is True
    # SHORT with edge → allow
    allowed_short, _ = filter_duration_for_capital(
        capital_total=540.0, duration_bucket="SHORT",
        ev_lower_bound=0.10, capital_turnover_score=20.0,
    )
    assert allowed_short is True
    # MEDIUM low edge → reject
    rej_med, _ = filter_duration_for_capital(
        capital_total=540.0, duration_bucket="MEDIUM",
        ev_lower_bound=0.10, capital_turnover_score=20.0,
    )
    assert rej_med is False
    # MEDIUM high edge → allow
    allowed_med, _ = filter_duration_for_capital(
        capital_total=540.0, duration_bucket="MEDIUM",
        ev_lower_bound=0.25, capital_turnover_score=35.0,
    )
    assert allowed_med is True
    # LONG and VERY_LONG always reject at MEDIUM
    for bucket in ("LONG", "VERY_LONG"):
        rej, _ = filter_duration_for_capital(
            capital_total=540.0, duration_bucket=bucket,
            ev_lower_bound=0.30, capital_turnover_score=50.0,
        )
        assert rej is False


def test_b13_no_6_day_trade_at_100eur():
    """Sanity test reproducing the 3 ELITE LONG-Sport trades found in
    the 2026-05-03 smoke that incorrectly passed B13 pre-fix.

    market: Sport, expected_resolution_minutes ≈ 9000 (= 150 hours = ~6 days),
    bucket=LONG, high composite_score (ev_lb≈0.95).

    Pre-fix: this passed CAPITAL_LOCK_TOO_LONG and locked $7.50 for 6 days.
    Post-fix: must reject regardless of EV."""
    # exp_min ~ 8930 → bucket LONG (1440-10080)
    bucket = classify_duration(8930.0)
    assert bucket == "LONG"
    # Compute the actual ev_per_hour and turnover the way the allocator does
    ev_per_h = compute_ev_per_hour(0.95, 8930.0)
    lock_pen = compute_capital_lock_penalty(8930.0, 108.0)
    turnover = compute_capital_turnover_score(ev_per_h, lock_pen)
    # Filter rejects regardless of EV/turnover at Tier 1
    allowed, reason = filter_duration_for_capital(
        capital_total=108.0,
        duration_bucket=bucket,
        ev_lower_bound=0.95,
        capital_turnover_score=turnover,
    )
    assert allowed is False, "100€ must NEVER allow a 6-day Sport trade"
    assert reason == "CAPITAL_LOCK_TOO_LONG"


def test_capital_lock_penalty_high_for_small_capital():
    """A 6h lock is much harsher at 50€ than at 5000€."""
    pen_50 = compute_capital_lock_penalty(360, 50.0)
    pen_500 = compute_capital_lock_penalty(360, 500.0)
    pen_5000 = compute_capital_lock_penalty(360, 5000.0)
    assert pen_50 > pen_500
    assert pen_500 > pen_5000
    assert pen_5000 == 0.0
    # 6h on 50€ scale (1h denominator) -> max penalty 1.0
    assert pen_50 == 1.0


def test_ev_per_hour_calculated():
    """EV/hour with 5-min floor on minutes."""
    # 30 min -> 0.5h; ev=0.05 -> 0.10/h
    assert abs(compute_ev_per_hour(0.05, 30) - 0.10) < 1e-9
    # 60 min -> 1h; ev=0.05 -> 0.05/h
    assert abs(compute_ev_per_hour(0.05, 60) - 0.05) < 1e-9
    # 0 min (edge case) -> floored to 5 min -> 0.05/(5/60) = 0.6
    val = compute_ev_per_hour(0.05, 0)
    assert val > 0.5  # roughly 0.6
    # very long (10080 min = 1 week)
    weekly = compute_ev_per_hour(0.05, 10080)
    assert weekly < 0.001  # ev/hour very small


def test_classify_duration_buckets():
    assert classify_duration(2) == "ULTRA_SHORT"
    assert classify_duration(15) == "VERY_SHORT"
    assert classify_duration(60) == "SHORT"
    assert classify_duration(300) == "MEDIUM"
    assert classify_duration(2880) == "LONG"
    assert classify_duration(15000) == "VERY_LONG"
    assert classify_duration(None) == "VERY_LONG"  # unknown defaults worst


def test_turnover_score_clamped_0_to_100():
    """ev_per_hour=0.10 + zero penalty -> 100 (clipped at upper bound)."""
    score = compute_capital_turnover_score(0.10, 0.0)
    assert 0.0 <= score <= 100.0
    assert score == 100.0
    # Negative input scenario -> floored at 0
    score_neg = compute_capital_turnover_score(0.0, 1.0)
    assert score_neg >= 0.0
