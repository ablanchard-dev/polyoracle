"""Hot lane classifier — 3-tier scheduling priority for polling.

CONTEXTE
========

Le polling uniforme (cycle 49s sur 733 wallets) rate 68% des wallets ACTIFS
sur Polymarket (validé fresh API 2026-05-17, 92/135 wallets). Cause : leur
fréquence de trade > notre cycle polling.

Le hot lane scheduler classifie chaque wallet en 3 lanes :
- HOT  (10s)  : wallets très actifs récemment
- WARM (30s)  : wallets actifs 24h-7j
- COLD (120s) : dormants / WATCHLIST candidates

Avantage : on poll plus souvent les wallets qui produisent, moins les dormants.
Budget total ~12.6 c/s sous le cap 15 c/s (cohort 733).

DOCTRINE
========

- Aucun changement audit / edge / sizing / risk
- Aucun bypass paper=live
- Pure classification + scheduling
- Feature flag SCHEDULER_HOT_LANE_ENABLED (default false)
- Fallback uniforme si exception

CLASSIFICATION SOURCES
======================

Multi-source pour ne pas dépendre uniquement de notre paper history (sinon
on rate les 92 ACTIVE_NOT_SEEN_BY_US) :

| Signal | Weight | Source |
|---|---|---|
| paper trade chez nous <1h | 1.0 | papertrade |
| signal détecté <1h | 0.8 | notradedecision |
| fresh API trade <1h | 1.0 | data-api `/trades?user` |
| paper trade <24h | 0.6 | papertrade |
| signal détecté <24h | 0.4 | notradedecision |
| fresh API trade <7d | 0.4 | data-api |
| recent_activity_score >70 | 0.3 | MFWR |
| recent_activity_score 25-70 | 0.1 | MFWR |
| Rien | 0.0 | — |

Score HOT >=0.8 ; WARM >=0.3 ; COLD <0.3

REFRESH POLICY
==============

- Re-classify each wallet every 5 minutes (lightweight DB queries)
- Fresh API check weekly cron (cold wallets only, 100 wallets/day ≈ 1c/s)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from sqlmodel import Session, select, text

LANE_HOT = "HOT"
LANE_WARM = "WARM"
LANE_COLD = "COLD"

Lane = Literal["HOT", "WARM", "COLD"]

DEFAULT_INTERVAL_HOT_S = 10.0
DEFAULT_INTERVAL_WARM_S = 60.0  # 2026-05-17: bumped 30s -> 60s (budget tuning, 419 WARM = 6.98 c/s)
DEFAULT_INTERVAL_COLD_S = 120.0

HOT_SCORE_THRESHOLD = 0.8
WARM_SCORE_THRESHOLD = 0.3


@dataclass
class WalletActivitySnapshot:
    """Activity signals snapshot for a wallet at a point in time."""
    address: str
    paper_trade_within_1h: bool = False
    paper_trade_within_24h: bool = False
    signal_within_1h: bool = False
    signal_within_24h: bool = False
    fresh_api_trade_within_1h: bool = False
    fresh_api_trade_within_7d: bool = False
    recent_activity_score: float = 0.0  # MFWR field 0-100

    def compute_score(self) -> float:
        """Tightened scoring (2026-05-17 v2) : HOT requires real <1h activity.

        Previous version allowed (paper_24h + signal_24h + ras>=70) = score 1.3 = HOT,
        producing 129 HOT (17%) on 747 cohort = budget 33 c/s OVER cap 15.

        New : HOT requires confirmed recent activity (paper_1h, signal_1h, or fresh_1h).
        WARM = activity within 24h. COLD = older or nothing.
        """
        score = 0.0
        # HOT signals (require recency <1h to qualify)
        if self.paper_trade_within_1h:
            score += 1.0
        if self.signal_within_1h:
            score += 0.8
        if self.fresh_api_trade_within_1h:
            score += 1.0
        # WARM signals (only count if no 1h activity)
        if not (self.paper_trade_within_1h or self.signal_within_1h):
            if self.paper_trade_within_24h:
                score += 0.3
            if self.signal_within_24h:
                score += 0.2
            if self.fresh_api_trade_within_7d:
                score += 0.3
        # Recent activity score MFWR (small bonus)
        if self.recent_activity_score >= 70:
            score += 0.1
        elif self.recent_activity_score >= 25:
            score += 0.05
        return score

    def classify(self) -> Lane:
        score = self.compute_score()
        if score >= HOT_SCORE_THRESHOLD:
            return LANE_HOT
        if score >= WARM_SCORE_THRESHOLD:
            return LANE_WARM
        return LANE_COLD


class HotLaneClassifier:
    """Classify wallets into HOT/WARM/COLD lanes for polling scheduling.

    Designed to be safe even if DB queries fail (returns WARM as default safe).
    """

    def __init__(
        self,
        session: Session,
        interval_hot_s: float = DEFAULT_INTERVAL_HOT_S,
        interval_warm_s: float = DEFAULT_INTERVAL_WARM_S,
        interval_cold_s: float = DEFAULT_INTERVAL_COLD_S,
    ) -> None:
        self.session = session
        self.interval_hot_s = interval_hot_s
        self.interval_warm_s = interval_warm_s
        self.interval_cold_s = interval_cold_s

    def snapshot_from_db(self, address: str, now: Optional[datetime] = None) -> WalletActivitySnapshot:
        """Build activity snapshot from DB queries (read-only).

        FIX 2026-05-17: SQLite datetimes are stored without TZ ('YYYY-MM-DD HH:MM:SS.ffffff').
        Use strftime to match, OR use SQLite's datetime('now') directly.
        """
        snap = WalletActivitySnapshot(address=address)

        try:
            # Use SQLite datetime('now', '-N hours') for native compat
            r = self.session.exec(text(
                "SELECT 1 FROM papertrade WHERE wallet_address = :a AND opened_at >= datetime('now', '-1 hour') LIMIT 1"
            ).bindparams(a=address)).first()
            snap.paper_trade_within_1h = r is not None

            r = self.session.exec(text(
                "SELECT 1 FROM papertrade WHERE wallet_address = :a AND opened_at >= datetime('now', '-24 hours') LIMIT 1"
            ).bindparams(a=address)).first()
            snap.paper_trade_within_24h = r is not None

            r = self.session.exec(text(
                "SELECT 1 FROM notradedecision WHERE wallet_address = :a AND created_at >= datetime('now', '-1 hour') LIMIT 1"
            ).bindparams(a=address)).first()
            snap.signal_within_1h = r is not None

            r = self.session.exec(text(
                "SELECT 1 FROM notradedecision WHERE wallet_address = :a AND created_at >= datetime('now', '-24 hours') LIMIT 1"
            ).bindparams(a=address)).first()
            snap.signal_within_24h = r is not None

            # recent_activity_score from MFWR
            r = self.session.exec(text(
                "SELECT recent_activity_score FROM marketfirstwalletrecord WHERE address = :a"
            ).bindparams(a=address)).first()
            if r and r[0] is not None:
                snap.recent_activity_score = float(r[0])

        except Exception:
            # safe fallback
            pass

        return snap

    def classify(self, address: str, now: Optional[datetime] = None,
                 fresh_api_1h: bool = False, fresh_api_7d: bool = False) -> Lane:
        """Return lane HOT/WARM/COLD for wallet."""
        snap = self.snapshot_from_db(address, now)
        snap.fresh_api_trade_within_1h = fresh_api_1h
        snap.fresh_api_trade_within_7d = fresh_api_7d
        return snap.classify()

    def get_interval_for_lane(self, lane: Lane) -> float:
        if lane == LANE_HOT:
            return self.interval_hot_s
        if lane == LANE_WARM:
            return self.interval_warm_s
        return self.interval_cold_s


def estimate_lane_budget(
    cohort_size: int,
    hot_count: int,
    warm_count: int,
    cold_count: int,
    interval_hot_s: float = DEFAULT_INTERVAL_HOT_S,
    interval_warm_s: float = DEFAULT_INTERVAL_WARM_S,
    interval_cold_s: float = DEFAULT_INTERVAL_COLD_S,
) -> dict:
    """Compute API budget c/s for a given lane distribution.

    Returns dict with calls_per_sec for each lane + total.
    """
    hot_cps = hot_count / interval_hot_s if interval_hot_s > 0 else 0
    warm_cps = warm_count / interval_warm_s if interval_warm_s > 0 else 0
    cold_cps = cold_count / interval_cold_s if interval_cold_s > 0 else 0
    total = hot_cps + warm_cps + cold_cps
    return {
        "cohort_size": cohort_size,
        "hot_count": hot_count,
        "warm_count": warm_count,
        "cold_count": cold_count,
        "hot_cps": round(hot_cps, 2),
        "warm_cps": round(warm_cps, 2),
        "cold_cps": round(cold_cps, 2),
        "total_cps": round(total, 2),
        "under_cap_15": total <= 15.0,
    }
