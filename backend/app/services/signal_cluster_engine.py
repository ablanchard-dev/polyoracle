"""SignalClusterEngine — collapse N wallet signals on the same
market+outcome+side into ONE logical trade decision.

Why
---
With ~981 tradable wallets, dozens may pile onto the same outcome inside
a short window. Naively opening N positions explodes per-market exposure.
Bonne logique: a *cluster* (market_id + outcome + side) accumulates
confirming wallets; a single trade is triggered when consensus is strong
enough; subsequent wallets either pyramid (if edge survives) or are
ignored.

The engine is a pure in-memory state machine. It never hits the DB or
API directly; the caller (paper_trading_engine) feeds it signals and
acts on the returned ``ClusterEvent``.

Cluster lifecycle
-----------------
* ``PENDING``     — at least one wallet has signaled, threshold not yet met
* ``TRIGGERED``   — consensus hit, paper position opened
* ``EXPIRED``     — TTL elapsed or late_crowd_trap fired before triggering
* ``CONFLICTING`` — opposite-side cluster on the same market is TRIGGERED

Six action codes the engine emits (all logged to NoTradeDecisionLog by the
caller — *DUPLICATE / PYRAMID_DENIED / CONFLICTING / LATE_CROWD_TRAP*
become reason_codes; *TRIGGER / NEW / PYRAMID_OK* feed the allocator).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable


# Tunables — exposed for tests and runtime overrides.
DEFAULT_TTL_SECONDS = 1800  # 30 min: cluster expires if not triggered
# v0.7.2 B7-B: raised 0.05 -> 0.15 — Polymarket binary markets routinely move
# 5-30% within a cluster window; 5% killed every cluster as LATE_CROWD_TRAP.
DEFAULT_PRICE_DRIFT_TRAP_PCT = 0.15
DEFAULT_PYRAMID_DRIFT_MAX_PCT = 0.02  # pyramid only if midpoint within 2% of avg entry
DEFAULT_MAX_PYRAMID_ADDITIONS = 2  # cap subsequent ELITE adds beyond initial trigger
# v0.7.2 B7-A: bumped CANDIDATE_ELITE 0.25 -> 0.4, added WATCH 0.3 — single
# ELITE still triggers alone (1.0 == threshold), 2 STRONG still trigger (1.0),
# combinations now reach threshold faster (1 ELITE + 1 anything > 1.0).
TIER_WEIGHTS = {
    "ELITE": 1.0,
    "STRONG": 0.5,
    "CANDIDATE_ELITE": 0.4,
    "WATCH": 0.3,
}
DEFAULT_TRIGGER_THRESHOLD = 1.0  # 1 ELITE OR 2 STRONG (0.5 + 0.5)


# Action codes the caller may receive.
ACTION_NEW_CLUSTER = "NEW_CLUSTER"
ACTION_DUPLICATE_SAME_WALLET = "DUPLICATE_SAME_WALLET"
ACTION_CONSENSUS_BUILDING = "CONSENSUS_BUILDING"  # added wallet, threshold not met yet
ACTION_TRIGGER = "TRIGGER"  # consensus met → caller opens paper position
ACTION_PYRAMID_OK = "PYRAMID_ADD_CONFIRMED_EDGE"  # add to existing position
ACTION_PYRAMID_DENIED = "DUPLICATE_OR_PYRAMID_DENIED"  # cluster triggered, edge gone
ACTION_CONFLICTING = "CONFLICTING_SMART_WALLET_SIGNAL"  # opposite side already triggered
ACTION_LATE_CROWD_TRAP = "LATE_CROWD_TRAP"  # price drifted too far from earliest entry
ACTION_EXPIRED = "EXPIRED"  # cluster TTL elapsed without triggering


@dataclass
class WalletSnapshot:
    address: str
    tier: str
    entry_price: float
    entry_at: datetime
    notional_usd: float = 0.0


@dataclass
class SignalCluster:
    cluster_id: str
    market_id: str
    outcome: str
    side: str
    wallets_confirming: list[WalletSnapshot] = field(default_factory=list)
    consensus_score: float = 0.0
    avg_entry_price_wallets: float = 0.0
    earliest_entry_at: datetime | None = None
    latest_entry_at: datetime | None = None
    current_midpoint: float = 0.0
    price_deterioration: float = 0.0
    state: str = "PENDING"
    triggered_position_id: str | None = None
    cluster_classification: str = "PENDING"
    pyramid_additions: int = 0  # count of PYRAMID_OK events accepted

    def has_wallet(self, address: str) -> bool:
        addr = address.lower()
        return any(w.address.lower() == addr for w in self.wallets_confirming)

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "market_id": self.market_id,
            "outcome": self.outcome,
            "side": self.side,
            "n_wallets": len(self.wallets_confirming),
            "wallets": [w.address for w in self.wallets_confirming],
            "consensus_score": self.consensus_score,
            "avg_entry_price_wallets": self.avg_entry_price_wallets,
            "current_midpoint": self.current_midpoint,
            "price_deterioration": self.price_deterioration,
            "state": self.state,
            "triggered_position_id": self.triggered_position_id,
            "cluster_classification": self.cluster_classification,
        }


@dataclass
class IncomingSignal:
    """Lean signal payload the cluster engine consumes."""

    wallet_address: str
    wallet_tier: str
    market_id: str
    outcome: str
    side: str
    entry_price: float
    notional_usd: float = 0.0
    current_midpoint: float | None = None
    timestamp: datetime | None = None

    @property
    def at(self) -> datetime:
        return self.timestamp or datetime.now(timezone.utc)


@dataclass
class ClusterEvent:
    action: str
    cluster: SignalCluster
    reason: str = ""
    trigger_now: bool = False
    pyramid_now: bool = False


class SignalClusterEngine:
    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        price_drift_trap_pct: float = DEFAULT_PRICE_DRIFT_TRAP_PCT,
        pyramid_drift_max_pct: float = DEFAULT_PYRAMID_DRIFT_MAX_PCT,
        trigger_threshold: float = DEFAULT_TRIGGER_THRESHOLD,
        max_pyramid_additions: int = DEFAULT_MAX_PYRAMID_ADDITIONS,
        tier_weights: dict[str, float] | None = None,
    ) -> None:
        self.clusters: dict[str, SignalCluster] = {}
        self.ttl_seconds = ttl_seconds
        self.price_drift_trap_pct = price_drift_trap_pct
        self.pyramid_drift_max_pct = pyramid_drift_max_pct
        self.trigger_threshold = trigger_threshold
        self.max_pyramid_additions = max_pyramid_additions
        self.tier_weights = dict(tier_weights or TIER_WEIGHTS)

    # ---------- helpers ----------

    def cluster_id_for(self, market_id: str, outcome: str, side: str) -> str:
        key = f"{market_id.lower()}|{outcome.lower()}|{side.upper()}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def opposite_side(self, side: str) -> str:
        return "SELL" if side.upper() == "BUY" else "BUY"

    def _tier_weight(self, tier: str) -> float:
        return self.tier_weights.get(tier.upper(), 0.0)

    def _recompute_aggregates(self, cluster: SignalCluster) -> None:
        if not cluster.wallets_confirming:
            cluster.consensus_score = 0.0
            cluster.avg_entry_price_wallets = 0.0
            cluster.earliest_entry_at = None
            cluster.latest_entry_at = None
            return
        cluster.consensus_score = sum(
            self._tier_weight(w.tier) for w in cluster.wallets_confirming
        )
        cluster.avg_entry_price_wallets = sum(
            w.entry_price for w in cluster.wallets_confirming
        ) / len(cluster.wallets_confirming)
        cluster.earliest_entry_at = min(w.entry_at for w in cluster.wallets_confirming)
        cluster.latest_entry_at = max(w.entry_at for w in cluster.wallets_confirming)

    def _drift_pct(self, midpoint: float, reference: float) -> float:
        if reference <= 0:
            return 0.0
        return abs(midpoint - reference) / reference

    # ---------- API ----------

    def add_signal(self, signal: IncomingSignal) -> ClusterEvent:
        """Ingest a wallet signal. Returns a ClusterEvent the caller must
        translate into either a CapitalAllocator call (TRIGGER / PYRAMID_OK)
        or a NoTradeDecisionLog entry (DUPLICATE / PYRAMID_DENIED / CONFLICTING
        / LATE_CROWD_TRAP / EXPIRED / CONSENSUS_BUILDING).
        """
        cid = self.cluster_id_for(signal.market_id, signal.outcome, signal.side)
        opp_cid = self.cluster_id_for(
            signal.market_id, signal.outcome, self.opposite_side(signal.side)
        )

        # 1) Conflicting signal: opposite-side already triggered
        opposite = self.clusters.get(opp_cid)
        if opposite and opposite.state == "TRIGGERED":
            opposite.cluster_classification = "CONFLICTING_SMART_WALLET_SIGNAL"
            return ClusterEvent(
                action=ACTION_CONFLICTING,
                cluster=opposite,
                reason=(
                    f"opposite-side cluster already TRIGGERED "
                    f"(side={opposite.side}, n_wallets={len(opposite.wallets_confirming)})"
                ),
            )

        existing = self.clusters.get(cid)
        midpoint = signal.current_midpoint if signal.current_midpoint is not None else signal.entry_price

        # 2) Existing cluster
        if existing is not None:
            if existing.has_wallet(signal.wallet_address):
                return ClusterEvent(
                    action=ACTION_DUPLICATE_SAME_WALLET,
                    cluster=existing,
                    reason="same wallet already in cluster — no-op",
                )

            existing.current_midpoint = midpoint

            # 2a) Already triggered — pyramid?
            if existing.state == "TRIGGERED":
                drift = self._drift_pct(midpoint, existing.avg_entry_price_wallets)
                existing.price_deterioration = drift
                pyramid_cap_hit = existing.pyramid_additions >= self.max_pyramid_additions
                if (
                    drift <= self.pyramid_drift_max_pct
                    and signal.wallet_tier.upper() == "ELITE"
                    and not pyramid_cap_hit
                ):
                    existing.wallets_confirming.append(
                        WalletSnapshot(
                            address=signal.wallet_address.lower(),
                            tier=signal.wallet_tier.upper(),
                            entry_price=signal.entry_price,
                            entry_at=signal.at,
                            notional_usd=signal.notional_usd,
                        )
                    )
                    existing.pyramid_additions += 1
                    self._recompute_aggregates(existing)
                    existing.cluster_classification = "PYRAMID_ADD_CONFIRMED_EDGE"
                    return ClusterEvent(
                        action=ACTION_PYRAMID_OK,
                        cluster=existing,
                        reason=(
                            f"ELITE add within {drift:.4f} drift "
                            f"(pyramid #{existing.pyramid_additions}/{self.max_pyramid_additions})"
                        ),
                        pyramid_now=True,
                    )
                # Pyramid denied (cap hit, drift too far, or wallet not ELITE)
                existing.cluster_classification = "DUPLICATE_OR_PYRAMID_DENIED"
                if pyramid_cap_hit:
                    reason = (
                        f"pyramid cap reached: {existing.pyramid_additions} adds "
                        f">= max {self.max_pyramid_additions}"
                    )
                else:
                    reason = (
                        f"cluster already triggered; "
                        f"drift={drift:.4f} > {self.pyramid_drift_max_pct} "
                        f"or wallet tier={signal.wallet_tier} not ELITE"
                    )
                return ClusterEvent(
                    action=ACTION_PYRAMID_DENIED,
                    cluster=existing,
                    reason=reason,
                )

            # 2b) PENDING but late_crowd_trap?
            if existing.earliest_entry_at is not None:
                ttl_elapsed = (signal.at - existing.earliest_entry_at).total_seconds()
                drift = self._drift_pct(midpoint, existing.avg_entry_price_wallets)
                existing.price_deterioration = drift
                if ttl_elapsed >= self.ttl_seconds:
                    existing.state = "EXPIRED"
                    existing.cluster_classification = "EXPIRED_TTL"
                    return ClusterEvent(
                        action=ACTION_EXPIRED,
                        cluster=existing,
                        reason=f"TTL {self.ttl_seconds}s elapsed since earliest entry",
                    )
                if drift > self.price_drift_trap_pct:
                    existing.state = "EXPIRED"
                    existing.cluster_classification = "LATE_CROWD_TRAP"
                    return ClusterEvent(
                        action=ACTION_LATE_CROWD_TRAP,
                        cluster=existing,
                        reason=(
                            f"price drift {drift:.4f} > {self.price_drift_trap_pct} "
                            f"since earliest entry — too late, edge eaten"
                        ),
                    )

            # 2c) Pending — accumulate
            existing.wallets_confirming.append(
                WalletSnapshot(
                    address=signal.wallet_address.lower(),
                    tier=signal.wallet_tier.upper(),
                    entry_price=signal.entry_price,
                    entry_at=signal.at,
                    notional_usd=signal.notional_usd,
                )
            )
            self._recompute_aggregates(existing)
            if existing.consensus_score >= self.trigger_threshold:
                existing.cluster_classification = "SMART_CONSENSUS_CLUSTER"
                return ClusterEvent(
                    action=ACTION_TRIGGER,
                    cluster=existing,
                    reason=(
                        f"consensus_score={existing.consensus_score} ≥ {self.trigger_threshold} "
                        f"(n_wallets={len(existing.wallets_confirming)})"
                    ),
                    trigger_now=True,
                )
            existing.cluster_classification = "PENDING"
            return ClusterEvent(
                action=ACTION_CONSENSUS_BUILDING,
                cluster=existing,
                reason=(
                    f"consensus_score={existing.consensus_score} < {self.trigger_threshold}"
                ),
            )

        # 3) New cluster
        cluster = SignalCluster(
            cluster_id=cid,
            market_id=signal.market_id.lower(),
            outcome=signal.outcome,
            side=signal.side.upper(),
            current_midpoint=midpoint,
        )
        cluster.wallets_confirming.append(
            WalletSnapshot(
                address=signal.wallet_address.lower(),
                tier=signal.wallet_tier.upper(),
                entry_price=signal.entry_price,
                entry_at=signal.at,
                notional_usd=signal.notional_usd,
            )
        )
        self._recompute_aggregates(cluster)
        self.clusters[cid] = cluster
        if cluster.consensus_score >= self.trigger_threshold:
            cluster.cluster_classification = "SMART_CONSENSUS_CLUSTER"
            return ClusterEvent(
                action=ACTION_TRIGGER,
                cluster=cluster,
                reason=(
                    f"single-wallet trigger: consensus_score={cluster.consensus_score} "
                    f"≥ {self.trigger_threshold}"
                ),
                trigger_now=True,
            )
        cluster.cluster_classification = "PENDING"
        return ClusterEvent(
            action=ACTION_NEW_CLUSTER,
            cluster=cluster,
            reason=(
                f"new cluster, consensus_score={cluster.consensus_score} "
                f"< {self.trigger_threshold}"
            ),
        )

    def mark_triggered(self, cluster_id: str, paper_position_id: str) -> None:
        cluster = self.clusters.get(cluster_id)
        if cluster is None:
            return
        cluster.state = "TRIGGERED"
        cluster.triggered_position_id = paper_position_id

    def expire_old(self, now: datetime | None = None) -> list[SignalCluster]:
        now = now or datetime.now(timezone.utc)
        expired: list[SignalCluster] = []
        for cluster in self.clusters.values():
            if cluster.state != "PENDING" or cluster.earliest_entry_at is None:
                continue
            if (now - cluster.earliest_entry_at).total_seconds() >= self.ttl_seconds:
                cluster.state = "EXPIRED"
                cluster.cluster_classification = "EXPIRED_TTL"
                expired.append(cluster)
        return expired

    def reset(self) -> None:
        self.clusters.clear()

    def all_clusters(self) -> Iterable[SignalCluster]:
        return list(self.clusters.values())
