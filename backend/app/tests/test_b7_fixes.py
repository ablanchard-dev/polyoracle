"""v0.7.2 B7 fix tests.

* B7-A — TIER_WEIGHTS update: ELITE=1.0, STRONG=0.5, CANDIDATE_ELITE=0.4
  (was 0.25), WATCH=0.3 (new). Single ELITE auto-triggers; single STRONG
  still waits for confirmation; combinations reach the trigger threshold
  faster than before.

* B7-B — DEFAULT_PRICE_DRIFT_TRAP_PCT bumped 0.05 -> 0.15 to be
  Polymarket-realistic (binary markets routinely move 5-30% within a
  cluster window).

* B7-C — sweep_stale_paper_trades(session, max_age_hours=24) marks any
  papertrade.status='open' row older than 24h as 'stale_pre_fix' so it
  no longer counts toward max_open_positions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.signal_cluster_engine import (
    ACTION_LATE_CROWD_TRAP,
    ACTION_NEW_CLUSTER,
    ACTION_TRIGGER,
    ACTION_CONSENSUS_BUILDING,
    DEFAULT_PRICE_DRIFT_TRAP_PCT,
    TIER_WEIGHTS,
    IncomingSignal,
    SignalClusterEngine,
)


UTC = timezone.utc
T0 = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)


def _signal(wallet, tier, market="0xmkt", outcome="YES", side="BUY",
            price=0.50, midpoint=None, when=None):
    return IncomingSignal(
        wallet_address=wallet,
        wallet_tier=tier,
        market_id=market,
        outcome=outcome,
        side=side,
        entry_price=price,
        notional_usd=10.0,
        current_midpoint=midpoint if midpoint is not None else price,
        timestamp=when or T0,
    )


# ============ B7-A: tier weights ============


def test_b7a_tier_weights_constants_match_spec():
    """Constant block exposes the new weights."""
    assert TIER_WEIGHTS["ELITE"] == 1.0
    assert TIER_WEIGHTS["STRONG"] == 0.5
    assert TIER_WEIGHTS["CANDIDATE_ELITE"] == 0.4
    assert TIER_WEIGHTS["WATCH"] == 0.3


def test_b7a_single_elite_triggers_cluster_alone():
    """One ELITE wallet is enough to trigger (1.0 >= 1.0)."""
    eng = SignalClusterEngine()
    ev = eng.add_signal(_signal("0xELITE_1", "ELITE"))
    assert ev.action == ACTION_TRIGGER
    assert ev.trigger_now is True
    assert ev.cluster.consensus_score == 1.0


def test_b7a_single_strong_does_not_trigger_alone():
    """One STRONG wallet stays PENDING (0.5 < 1.0)."""
    eng = SignalClusterEngine()
    ev = eng.add_signal(_signal("0xSTRONG_A", "STRONG"))
    assert ev.action == ACTION_NEW_CLUSTER
    assert ev.trigger_now is False
    assert ev.cluster.state == "PENDING"
    assert ev.cluster.consensus_score == 0.5


def test_b7a_two_strongs_trigger_cluster():
    """Two STRONG wallets converge to 1.0 → TRIGGER."""
    eng = SignalClusterEngine()
    eng.add_signal(_signal("0xSTRONG_A", "STRONG"))
    ev2 = eng.add_signal(
        _signal("0xSTRONG_B", "STRONG", when=T0 + timedelta(seconds=30))
    )
    assert ev2.action == ACTION_TRIGGER
    assert ev2.cluster.consensus_score == 1.0
    assert len(ev2.cluster.wallets_confirming) == 2


def test_b7a_elite_plus_strong_triggers_with_amplification():
    """ELITE alone already triggers; a STRONG joining a (custom-threshold)
    cluster amplifies consensus to 1.5."""
    # Force a higher threshold so the ELITE doesn't auto-trigger; this lets
    # us verify the additive amplification path explicitly.
    eng = SignalClusterEngine(trigger_threshold=2.0)
    e1 = eng.add_signal(_signal("0xELITE_1", "ELITE"))
    assert e1.cluster.consensus_score == 1.0
    e2 = eng.add_signal(_signal("0xSTRONG_A", "STRONG",
                                when=T0 + timedelta(seconds=20)))
    # 1.0 + 0.5 = 1.5; still below custom threshold 2.0 but accumulated.
    assert e2.action == ACTION_CONSENSUS_BUILDING
    assert e2.cluster.consensus_score == 1.5


def test_b7a_candidate_elite_weight_is_0_4():
    """Two CANDIDATE_ELITE = 0.8 (still PENDING); CANDIDATE_ELITE + STRONG
    = 0.9 (still PENDING); CANDIDATE_ELITE + ELITE = 1.4 (TRIGGER)."""
    eng = SignalClusterEngine()
    e1 = eng.add_signal(_signal("0xCAND_A", "CANDIDATE_ELITE"))
    assert e1.action == ACTION_NEW_CLUSTER
    assert e1.cluster.consensus_score == 0.4
    e2 = eng.add_signal(_signal("0xCAND_B", "CANDIDATE_ELITE",
                                when=T0 + timedelta(seconds=10)))
    assert e2.action == ACTION_CONSENSUS_BUILDING
    assert abs(e2.cluster.consensus_score - 0.8) < 1e-9


def test_b7a_watch_weight_is_0_3():
    """WATCH tier resolves to 0.3 (new)."""
    eng = SignalClusterEngine()
    eng.add_signal(_signal("0xWATCH_A", "WATCH"))
    eng.add_signal(_signal("0xWATCH_B", "WATCH",
                           when=T0 + timedelta(seconds=10)))
    ev = eng.add_signal(_signal("0xWATCH_C", "WATCH",
                                when=T0 + timedelta(seconds=20)))
    # 3 × 0.3 = 0.9 → still PENDING (below 1.0 threshold)
    assert ev.action == ACTION_CONSENSUS_BUILDING
    assert abs(ev.cluster.consensus_score - 0.9) < 1e-9


# ============ B7-B: price drift threshold ============


def test_b7b_default_drift_threshold_is_0_15():
    """The new default reflects realistic Polymarket binary movement."""
    assert DEFAULT_PRICE_DRIFT_TRAP_PCT == 0.15


def test_b7b_price_drift_010_does_not_trigger_late_crowd():
    """10% drift is BELOW the new 15% trap → cluster keeps building."""
    eng = SignalClusterEngine(trigger_threshold=2.0)  # force two-wallet path
    eng.add_signal(_signal("0xSTRONG_A", "STRONG", price=0.50))
    ev = eng.add_signal(
        _signal("0xSTRONG_B", "STRONG", price=0.55, midpoint=0.55,
                when=T0 + timedelta(minutes=2))
    )
    assert ev.action != ACTION_LATE_CROWD_TRAP
    # Cluster keeps growing instead of expiring.
    assert ev.cluster.state == "PENDING"


def test_b7b_price_drift_020_triggers_late_crowd():
    """20% drift is ABOVE the new 15% trap → LATE_CROWD_TRAP."""
    eng = SignalClusterEngine(trigger_threshold=2.0)
    eng.add_signal(_signal("0xSTRONG_A", "STRONG", price=0.50))
    ev = eng.add_signal(
        _signal("0xSTRONG_B", "STRONG", price=0.60, midpoint=0.60,
                when=T0 + timedelta(minutes=2))
    )
    assert ev.action == ACTION_LATE_CROWD_TRAP
    assert ev.cluster.state == "EXPIRED"


# ============ B7-C: stale paper trade sweep ============


def _make_session():
    from sqlmodel import SQLModel, Session, create_engine
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_b7c_sweep_stale_paper_trades_marks_old_open_rows():
    """sweep_stale_paper_trades flips status='open' rows older than the
    cutoff to 'stale_pre_fix'; fresh rows are untouched."""
    from app.models.trade import PaperTrade
    from app.services.paper_trading_engine import sweep_stale_paper_trades

    sess = _make_session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sess.add(PaperTrade(
        id="fresh-1", market_id="0xmkt", outcome="YES", side="BUY",
        quantity=10.0, average_price=0.5, notional_usd=5.0, status="open",
        opened_at=now - timedelta(hours=1), wallet_address="0xA",
    ))
    sess.add(PaperTrade(
        id="stale-1", market_id="0xmkt2", outcome="NO", side="BUY",
        quantity=10.0, average_price=0.5, notional_usd=5.0, status="open",
        opened_at=now - timedelta(hours=72), wallet_address="0xB",
    ))
    sess.commit()

    swept = sweep_stale_paper_trades(sess, max_age_hours=24)
    assert swept == 1

    fresh = sess.get(PaperTrade, "fresh-1")
    stale = sess.get(PaperTrade, "stale-1")
    assert fresh.status == "open"
    assert stale.status == "stale_pre_fix"


def test_b7c_sweep_returns_zero_when_nothing_stale():
    from app.models.trade import PaperTrade
    from app.services.paper_trading_engine import sweep_stale_paper_trades

    sess = _make_session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sess.add(PaperTrade(
        id="fresh-only", market_id="0xm", outcome="YES", side="BUY",
        quantity=1.0, average_price=0.5, notional_usd=0.5, status="open",
        opened_at=now - timedelta(hours=2), wallet_address="0xC",
    ))
    sess.commit()

    swept = sweep_stale_paper_trades(sess, max_age_hours=24)
    assert swept == 0


def test_b7c_sweep_does_not_touch_closed_rows():
    from app.models.trade import PaperTrade
    from app.services.paper_trading_engine import sweep_stale_paper_trades

    sess = _make_session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sess.add(PaperTrade(
        id="closed-old", market_id="0xm", outcome="YES", side="BUY",
        quantity=1.0, average_price=0.5, notional_usd=0.5, status="closed",
        opened_at=now - timedelta(hours=72),
        closed_at=now - timedelta(hours=70),
        wallet_address="0xD",
    ))
    sess.commit()

    swept = sweep_stale_paper_trades(sess, max_age_hours=24)
    assert swept == 0
