"""Lean gates layer — remplace 6+ gates redondants legacy.

Conserve seulement les 4 gates essentiels qui ont une justification claire :
  1. kill_switch        — opérateur peut couper instantanément (touch file)
  2. capital_cap        — exposition max % capital ($X cap)
  3. cluster_dedup      — pas 2 trades simultanés sur même cluster wallets
  4. wallet_auto_mute   — wallet décaying (HyperDex pattern adapté)

SUPPRIMÉ :
  ✗ BAD_ORDERBOOK / WIDE_SPREAD — calibrés pour crypto perp tight, KO PM thin
  ✗ CAPITAL_LOCK_TOO_LONG — bloque les markets multi-jours qui sont l'edge PM
  ✗ min_copyable_edge — formule legacy (cross-platform mispricing 2-4% remplace)
  ✗ orderbook_quality require — redondant avec le signal OBI Phase B
  ✗ total_score / signal_score — accumulés vide, légiférés sans fondement

Toute décision = LeanGateDecision(allow: bool, reason: str, metadata: dict).
Une seule passe synchrone (pas de chain audit→signal→risk→allocator).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class LeanGateDecision:
    allow: bool
    reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class CopySignal:
    """Signal copy candidat (sorti du RTDSListener)."""
    trader: str                 # wallet source (lowercase)
    token_id: str               # outcome token ID Polymarket
    condition_id: str           # market condition_id (pour cluster dedup)
    side: str                   # "BUY" | "SELL"
    notional_usd: float         # taille trade source
    price: float                # prix d'entrée source
    ts_ms: int                  # timestamp WS event arrival
    source_ts_ms: int = 0       # timestamp dans le trade lui-même (RTDS)
    market_title: str = ""      # titre marché (debug)


class LeanGates:
    """Gates minimaux. Garde les 4 essentiels, jette le reste."""

    # Defaults Phase A — calibrer Phase E
    DEFAULT_MAX_CONCURRENT = 8           # max positions ouvertes en simultané
    DEFAULT_MAX_NOTIONAL_PER_TRADE = 25  # $ max par trade (NANO $100 capital → 25%)
    DEFAULT_MAX_TOTAL_EXPOSURE = 80      # $ exposition cumulée max (80% capital)
    DEFAULT_CLUSTER_DEDUP_WINDOW_S = 120 # 2 min same condition_id = 1 trade only
    DEFAULT_AUTO_MUTE_MIN_LOSSES = 5     # n_losses ≥ threshold AND total_pnl<-$1
    DEFAULT_AUTO_MUTE_MAX_PNL = -1.0

    def __init__(
        self,
        kill_switch_path: Path = Path("/tmp/polyoracle_v2_kill"),
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_notional_per_trade: float = DEFAULT_MAX_NOTIONAL_PER_TRADE,
        max_total_exposure: float = DEFAULT_MAX_TOTAL_EXPOSURE,
        cluster_dedup_window_s: float = DEFAULT_CLUSTER_DEDUP_WINDOW_S,
        wallet_perf=None,  # optional WalletPerformanceTracker (Phase D)
        cluster_engine=None,  # optional signal_cluster_engine instance
    ):
        self.kill_switch_path = kill_switch_path
        self.max_concurrent = max_concurrent
        self.max_notional_per_trade = max_notional_per_trade
        self.max_total_exposure = max_total_exposure
        self.cluster_dedup_window_s = cluster_dedup_window_s
        self.wallet_perf = wallet_perf
        self.cluster_engine = cluster_engine

        # cluster dedup state : condition_id -> last_open_ts_s
        self._recent_clusters: dict[str, float] = {}
        # muted wallets (operator-set OR auto-set via wallet_perf)
        self._muted: set[str] = set()

        # Stats observabilité
        self.stats = {
            "evaluated": 0,
            "passed": 0,
            "rejected_kill": 0,
            "rejected_concurrent": 0,
            "rejected_exposure": 0,
            "rejected_notional": 0,
            "rejected_cluster_dedup": 0,
            "rejected_muted": 0,
        }

    # ─── 1. Kill switch ───────────────────────────────────────────
    def _kill_switch_active(self) -> bool:
        return self.kill_switch_path.exists()

    # ─── 2. Capital cap ───────────────────────────────────────────
    def _concurrent_ok(self, n_open: int) -> bool:
        return n_open < self.max_concurrent

    def _notional_ok(self, notional: float) -> bool:
        return 0 < notional <= self.max_notional_per_trade

    def _exposure_ok(self, current_exposure: float, add_notional: float) -> bool:
        return (current_exposure + add_notional) <= self.max_total_exposure

    # ─── 3. Cluster dedup ─────────────────────────────────────────
    def _cluster_dedup_ok(self, condition_id: str) -> bool:
        """True si on n'a pas déjà ouvert sur ce market < dedup_window_s."""
        now = time.time()
        # Cleanup vieux entries
        self._recent_clusters = {
            k: v for k, v in self._recent_clusters.items()
            if (now - v) < self.cluster_dedup_window_s
        }
        return condition_id not in self._recent_clusters

    def mark_cluster_traded(self, condition_id: str):
        """À appeler après un trade exécuté pour bloquer dedup."""
        self._recent_clusters[condition_id] = time.time()

    # ─── 4. Wallet auto-mute ──────────────────────────────────────
    def mute(self, wallet: str, reason: str = "operator"):
        self._muted.add(wallet.lower())
        print(f"[GATE] MUTED {wallet[:14]} reason={reason}", flush=True)

    def unmute(self, wallet: str):
        self._muted.discard(wallet.lower())

    def _is_muted(self, wallet: str) -> bool:
        if wallet.lower() in self._muted:
            return True
        # Check wallet_perf si fourni
        if self.wallet_perf is not None:
            should_mute, reason = self.wallet_perf.should_auto_mute(wallet)
            if should_mute:
                self.mute(wallet, reason=f"auto:{reason}")
                return True
        return False

    # ─── Decision principale ──────────────────────────────────────
    def evaluate(
        self,
        signal: CopySignal,
        n_open: int = 0,
        current_exposure_usd: float = 0.0,
        target_notional_usd: Optional[float] = None,
    ) -> LeanGateDecision:
        """Évalue un signal contre les 4 gates. Retourne decision.

        target_notional_usd = taille qu'on va trader (≠ taille du source).
        Si None, fallback sur max_notional_per_trade (taille max).
        """
        self.stats["evaluated"] += 1
        meta = {
            "trader": signal.trader[:14],
            "condition_id": signal.condition_id[:12],
            "side": signal.side,
        }

        # Gate 1 : kill switch
        if self._kill_switch_active():
            self.stats["rejected_kill"] += 1
            return LeanGateDecision(False, "KILL_SWITCH_ACTIVE", meta)

        # Gate 2 : muted wallet
        if self._is_muted(signal.trader):
            self.stats["rejected_muted"] += 1
            return LeanGateDecision(False, "WALLET_MUTED", meta)

        # Gate 3a : concurrent cap
        if not self._concurrent_ok(n_open):
            self.stats["rejected_concurrent"] += 1
            return LeanGateDecision(
                False, f"MAX_CONCURRENT ({n_open}/{self.max_concurrent})", meta)

        # Gate 3b : notional / exposure
        notional = target_notional_usd if target_notional_usd is not None \
            else self.max_notional_per_trade
        if not self._notional_ok(notional):
            self.stats["rejected_notional"] += 1
            return LeanGateDecision(
                False, f"NOTIONAL_OOR (${notional:.2f})", meta)
        if not self._exposure_ok(current_exposure_usd, notional):
            self.stats["rejected_exposure"] += 1
            return LeanGateDecision(
                False,
                f"MAX_EXPOSURE (${current_exposure_usd:.0f}+${notional:.0f} > ${self.max_total_exposure})",
                meta,
            )

        # Gate 4 : cluster dedup
        if not self._cluster_dedup_ok(signal.condition_id):
            self.stats["rejected_cluster_dedup"] += 1
            return LeanGateDecision(
                False, "CLUSTER_DEDUP (recent same-market trade)", meta)

        self.stats["passed"] += 1
        return LeanGateDecision(True, "OK", meta)

    # ─── Reporting ────────────────────────────────────────────────
    def report(self) -> dict:
        ev = self.stats["evaluated"] or 1
        return {
            **self.stats,
            "pass_rate_pct": 100.0 * self.stats["passed"] / ev,
            "n_muted": len(self._muted),
            "n_recent_clusters": len(self._recent_clusters),
            "kill_active": self._kill_switch_active(),
        }


# ─── CLI smoke test ──────────────────────────────────────────────────
if __name__ == "__main__":
    gates = LeanGates(max_concurrent=3, max_notional_per_trade=25)
    test_sig = CopySignal(
        trader="0xabcd1234567890abcdef",
        token_id="42",
        condition_id="0xmkt001",
        side="BUY",
        notional_usd=100.0,
        price=0.55,
        ts_ms=int(time.time() * 1000),
        market_title="Will X happen?",
    )
    d1 = gates.evaluate(test_sig, n_open=0, current_exposure_usd=0, target_notional_usd=20)
    print(f"#1 (clean):     {d1}")
    gates.mark_cluster_traded(test_sig.condition_id)
    d2 = gates.evaluate(test_sig, n_open=0, current_exposure_usd=0, target_notional_usd=20)
    print(f"#2 (dedup):     {d2}")
    d3 = gates.evaluate(test_sig, n_open=10, current_exposure_usd=0, target_notional_usd=20)
    print(f"#3 (concurrent):{d3}")
    test_sig2 = CopySignal(**{**test_sig.__dict__, "condition_id": "0xmkt002"})
    d4 = gates.evaluate(test_sig2, n_open=0, current_exposure_usd=70, target_notional_usd=20)
    print(f"#4 (exposure):  {d4}")
    print(f"\nReport: {gates.report()}")
