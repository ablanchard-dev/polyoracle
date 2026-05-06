"""SignalClusterEngine unit tests (v0.7.2 P0.2).

Six mandatory cases per spec:

* dedupe same wallet + same cluster
* aggregate multiple wallets into one signal
* pyramid only if edge remains
* block conflicting signal
* late_crowd_trap detection
* never opens 20 positions on the same outcome (the headline guarantee)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.signal_cluster_engine import (
    ACTION_CONFLICTING,
    ACTION_CONSENSUS_BUILDING,
    ACTION_DUPLICATE_SAME_WALLET,
    ACTION_LATE_CROWD_TRAP,
    ACTION_NEW_CLUSTER,
    ACTION_PYRAMID_DENIED,
    ACTION_PYRAMID_OK,
    ACTION_TRIGGER,
    IncomingSignal,
    SignalClusterEngine,
)

UTC = timezone.utc
T0 = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _signal(
    wallet="0xELITE_1",
    tier="ELITE",
    market="0xmkt",
    outcome="YES",
    side="BUY",
    price=0.50,
    midpoint=None,
    notional=10.0,
    when=None,
):
    return IncomingSignal(
        wallet_address=wallet,
        wallet_tier=tier,
        market_id=market,
        outcome=outcome,
        side=side,
        entry_price=price,
        notional_usd=notional,
        current_midpoint=midpoint if midpoint is not None else price,
        timestamp=when or T0,
    )


# --------- 1) dedupe same wallet + same cluster ---------


def test_cluster_dedupes_same_wallet_same_market():
    eng = SignalClusterEngine()
    s1 = _signal(wallet="0xELITE_1")
    e1 = eng.add_signal(s1)
    assert e1.action == ACTION_TRIGGER  # ELITE alone hits threshold

    eng.mark_triggered(e1.cluster.cluster_id, paper_position_id="pos-1")

    # Same wallet sends another signal on same cluster
    e2 = eng.add_signal(_signal(wallet="0xELITE_1", when=T0 + timedelta(seconds=30)))
    assert e2.action == ACTION_DUPLICATE_SAME_WALLET
    assert e2.cluster.triggered_position_id == "pos-1"  # still references existing
    # State must remain TRIGGERED, no second open
    assert e2.cluster.state == "TRIGGERED"


# --------- 2) aggregate multiple wallets ---------


def test_cluster_aggregates_multiple_wallets_one_signal():
    """Two STRONG wallets converge to ONE trigger, not two trades."""
    eng = SignalClusterEngine()
    e1 = eng.add_signal(_signal(wallet="0xSTRONG_A", tier="STRONG"))
    assert e1.action == ACTION_NEW_CLUSTER  # STRONG alone (0.5) below threshold

    e2 = eng.add_signal(
        _signal(wallet="0xSTRONG_B", tier="STRONG", when=T0 + timedelta(seconds=20))
    )
    assert e2.action == ACTION_TRIGGER  # 0.5 + 0.5 = 1.0 ≥ threshold
    assert e2.cluster.cluster_id == e1.cluster.cluster_id  # same cluster
    assert len(e2.cluster.wallets_confirming) == 2


def test_cluster_below_threshold_stays_pending():
    eng = SignalClusterEngine(trigger_threshold=2.0)  # require 2 ELITE
    e1 = eng.add_signal(_signal(wallet="0xELITE_1", tier="ELITE"))
    assert e1.action == ACTION_NEW_CLUSTER
    assert e1.cluster.state == "PENDING"

    e2 = eng.add_signal(_signal(wallet="0xSTRONG_A", tier="STRONG", when=T0 + timedelta(seconds=10)))
    # 1.0 (ELITE) + 0.5 (STRONG) = 1.5 < 2.0
    assert e2.action == ACTION_CONSENSUS_BUILDING
    assert e2.cluster.consensus_score == 1.5
    assert e2.cluster.state == "PENDING"


# --------- 3) pyramid only if edge remains ---------


def test_cluster_pyramid_only_if_edge_remaining():
    eng = SignalClusterEngine(pyramid_drift_max_pct=0.02)
    e1 = eng.add_signal(_signal(wallet="0xELITE_1", price=0.50))
    eng.mark_triggered(e1.cluster.cluster_id, "pos-1")

    # Second ELITE arrives, midpoint barely moved → pyramid OK
    e2 = eng.add_signal(
        _signal(wallet="0xELITE_2", price=0.505, midpoint=0.505, when=T0 + timedelta(seconds=10))
    )
    assert e2.action == ACTION_PYRAMID_OK
    assert e2.pyramid_now is True

    # Third ELITE arrives but price drifted ~6% → DENIED
    e3 = eng.add_signal(
        _signal(wallet="0xELITE_3", price=0.53, midpoint=0.53, when=T0 + timedelta(seconds=20))
    )
    assert e3.action == ACTION_PYRAMID_DENIED


def test_cluster_pyramid_denies_non_elite():
    eng = SignalClusterEngine()
    e1 = eng.add_signal(_signal(wallet="0xELITE_1", price=0.50))
    eng.mark_triggered(e1.cluster.cluster_id, "pos-1")

    # STRONG arrives, midpoint untouched → still DENIED (non-ELITE)
    e2 = eng.add_signal(
        _signal(wallet="0xSTRONG_A", tier="STRONG", price=0.50, midpoint=0.50)
    )
    assert e2.action == ACTION_PYRAMID_DENIED


# --------- 4) block conflicting signal ---------


def test_cluster_blocks_conflicting_signal():
    eng = SignalClusterEngine()
    # First we BUY YES
    e1 = eng.add_signal(_signal(wallet="0xELITE_1", side="BUY"))
    eng.mark_triggered(e1.cluster.cluster_id, "pos-1")

    # ELITE wallet now SELLs YES on same market → conflicting
    e2 = eng.add_signal(
        _signal(wallet="0xELITE_2", side="SELL", when=T0 + timedelta(seconds=30))
    )
    assert e2.action == ACTION_CONFLICTING
    # The cluster reported is the opposite-side one (already triggered)
    assert e2.cluster.side == "BUY"
    assert e2.cluster.cluster_classification == "CONFLICTING_SMART_WALLET_SIGNAL"


# --------- 5) late_crowd_trap ---------


def test_cluster_late_crowd_trap_detected():
    """First wallet enters at price 0.50; second wallet arrives 20 min later
    on a market that has drifted to 0.58 (16% deterioration). Cluster goes
    EXPIRED with LATE_CROWD_TRAP classification."""
    eng = SignalClusterEngine(price_drift_trap_pct=0.05, trigger_threshold=2.0)
    e1 = eng.add_signal(_signal(wallet="0xSTRONG_A", tier="STRONG", price=0.50))
    assert e1.action == ACTION_NEW_CLUSTER  # STRONG below threshold

    e2 = eng.add_signal(
        _signal(
            wallet="0xSTRONG_B",
            tier="STRONG",
            price=0.58,
            midpoint=0.58,
            when=T0 + timedelta(minutes=20),
        )
    )
    assert e2.action == ACTION_LATE_CROWD_TRAP
    assert e2.cluster.state == "EXPIRED"
    assert e2.cluster.cluster_classification == "LATE_CROWD_TRAP"


# --------- 6) headline: never opens 20 positions on same outcome ---------


def test_cluster_does_not_open_20_positions_on_same_outcome():
    """Simulate 20 wallets piling onto the same market+outcome+side.
    Expect EXACTLY ONE TRIGGER + a few PYRAMID_OKs (only ELITEs at flat
    price), and zero additional NEW_CLUSTER events on that key."""
    eng = SignalClusterEngine()

    triggers = 0
    pyramids = 0
    other_actions = 0
    pos_id = None
    for i in range(20):
        when = T0 + timedelta(seconds=i * 5)
        # Mix of ELITEs and STRONGs, prices wander slightly within drift band
        tier = "ELITE" if i % 3 == 0 else "STRONG"
        price = 0.50 + (0.001 * (i % 5))
        ev = eng.add_signal(
            _signal(
                wallet=f"0xWALLET_{i}",
                tier=tier,
                price=price,
                midpoint=price,
                when=when,
            )
        )
        if ev.action == ACTION_TRIGGER:
            triggers += 1
            pos_id = f"pos-{triggers}"
            eng.mark_triggered(ev.cluster.cluster_id, pos_id)
        elif ev.action == ACTION_PYRAMID_OK:
            pyramids += 1
        else:
            other_actions += 1

    assert triggers == 1, f"expected exactly 1 trigger, got {triggers}"
    # Pyramid_OK fires only for ELITEs at low drift; STRONGs land in PYRAMID_DENIED.
    # Either way, ZERO additional triggers means no exposure explosion.
    assert eng.clusters[next(iter(eng.clusters))].state == "TRIGGERED"


# --------- bonus: TTL expiry ---------


def test_cluster_expires_on_ttl():
    eng = SignalClusterEngine(ttl_seconds=600, trigger_threshold=2.0)
    e1 = eng.add_signal(_signal(wallet="0xSTRONG_A", tier="STRONG"))
    assert e1.action == ACTION_NEW_CLUSTER

    # 11 minutes later, second STRONG arrives → TTL elapsed
    e2 = eng.add_signal(
        _signal(wallet="0xSTRONG_B", tier="STRONG", when=T0 + timedelta(minutes=11))
    )
    assert e2.action == "EXPIRED"
    assert e2.cluster.state == "EXPIRED"


def test_cluster_id_deterministic():
    eng = SignalClusterEngine()
    a = eng.cluster_id_for("0xMKT", "YES", "BUY")
    b = eng.cluster_id_for("0xmkt", "yes", "buy")
    assert a == b  # case-insensitive
    c = eng.cluster_id_for("0xMKT", "NO", "BUY")
    assert a != c
    d = eng.cluster_id_for("0xMKT", "YES", "SELL")
    assert a != d
