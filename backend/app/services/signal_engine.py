from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from app.config import get_settings
from app.models.signal import Signal, SignalType
from app.models.trade import PublicTrade, TradeAuditRecord, TradeCluster
from app.models.wallet import Wallet
from app.services.copyable_edge_engine import CopyableEdgeEngine
from app.services.mock_data import mock_signals
from app.services.smart_wallet_auditor import _safe_float

logger = logging.getLogger(__name__)


SIGNAL_DECISIONS = ("PAPER_TRADE", "WATCH", "REJECT", "NEED_MORE_DATA")


@dataclass
class SignalScore:
    wallet_quality: float = 0.0
    cluster: float = 0.0
    whale: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0
    orderbook: float = 0.0
    timing: float = 0.0
    copyable_edge: float = 0.0
    category_edge: float = 0.0
    confidence: float = 0.0
    risk_penalty: float = 0.0
    late_entry_penalty: float = 0.0
    total: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "wallet_quality": self.wallet_quality,
            "cluster": self.cluster,
            "whale": self.whale,
            "liquidity": self.liquidity,
            "spread": self.spread,
            "orderbook": self.orderbook,
            "timing": self.timing,
            "copyable_edge": self.copyable_edge,
            "category_edge": self.category_edge,
            "confidence": self.confidence,
            "risk_penalty": self.risk_penalty,
            "late_entry_penalty": self.late_entry_penalty,
            "total": self.total,
        }


class SignalEngine:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.edge_engine = CopyableEdgeEngine()

    # ---------------- legacy/mock entry points ----------------

    def generate_signals(self) -> list[Signal]:
        # 2026-05-07 leak fix: LIMIT 1000 most recent. Was loading all 138k
        # Signal rows on every /signals* hit → multi-MB/s leak under polling.
        try:
            stmt = select(Signal).order_by(Signal.created_at.desc()).limit(1000)
            persisted = list(self.session.exec(stmt).all())
        except Exception:
            persisted = []
        if persisted:
            return sorted(persisted, key=lambda signal: signal.score, reverse=True)
        return sorted(mock_signals(), key=lambda signal: signal.score, reverse=True)

    def list_signals(self, limit: int = 100) -> list[Signal]:
        try:
            statement = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
            return list(self.session.exec(statement).all())
        except Exception:
            return list(mock_signals())[:limit]

    def list_rejected(self, limit: int = 100) -> list[Signal]:
        try:
            statement = (
                select(Signal)
                .where(Signal.decision == "REJECT")
                .order_by(Signal.created_at.desc())
                .limit(limit)
            )
            return list(self.session.exec(statement).all())
        except Exception:
            return [signal for signal in mock_signals() if signal.status == "rejected"][:limit]

    def compute_signal_score(self, signal: Signal) -> float:
        return signal.score

    def explain_signal(self, signal: Signal) -> str:
        return signal.reason

    def reject_bad_signal(self, signal: Signal) -> bool:
        return signal.score < self.settings.min_signal_score or signal.status == "rejected"

    def detect_consensus(self) -> list[Signal]:
        return [signal for signal in self.generate_signals() if signal.signal_type == SignalType.SMART_CONSENSUS]

    def detect_whale_entry(self) -> list[Signal]:
        return [signal for signal in self.generate_signals() if signal.signal_type == SignalType.WHALE_ENTRY]

    def detect_exit_alert(self) -> list[Signal]:
        return [signal for signal in self.generate_signals() if signal.signal_type == SignalType.EXIT_ALERT]

    def detect_late_entry_risk(self) -> list[Signal]:
        return [
            signal
            for signal in self.generate_signals()
            if signal.signal_type in (SignalType.LATE_ENTRY_RISK, SignalType.LATE_CROWD_TRAP)
        ]

    def compute_copyable_edge(self, signal: Signal, spread: float = 0.01, slippage: float = 0.005, delay_penalty: float = 0.01) -> float:
        raw_edge = max(0.0, signal.score / 100 - 0.5)
        return round(max(0.0, raw_edge - spread - slippage - delay_penalty), 4)

    # ---------------- smart money signal generation ----------------

    def build_signal_from_trade_audit(self, audit: TradeAuditRecord) -> Signal:
        signal_type = self._signal_type_for_audit(audit)
        score = self._compute_audit_signal_score(audit)
        decision = self._derive_decision_from_audit(audit, score.total)
        reason = self._reason_for_audit(audit, signal_type)
        proposed_size = self._propose_size(audit)
        signal_id = f"sig-audit-{audit.id}"
        signal = Signal(
            id=signal_id,
            market_id=audit.market_id,
            outcome=(audit.outcome or "YES"),
            signal_type=signal_type,
            score=score.total,
            confidence=round(min(1.0, audit.wallet_score / 100), 4),
            reason=reason,
            action=decision.lower(),
            status=decision.lower(),
            decision=decision,
            wallets=json.dumps([audit.wallet_address]) if audit.wallet_address else None,
            cluster_id=None,
            proposed_size_usd=proposed_size,
            copyable_edge=audit.copyable_edge,
            notes=json.dumps(score.to_dict()),
        )
        self.session.merge(signal)
        self.session.commit()
        return self.session.get(Signal, signal_id) or signal

    def build_cluster_signal(self, cluster: TradeCluster) -> Signal:
        score = self._compute_cluster_signal_score(cluster)
        decision = "PAPER_TRADE" if score.total >= self.settings.min_signal_score and cluster.average_wallet_score >= 60 else "WATCH"
        signal_id = f"sig-cluster-{cluster.id}"
        signal = Signal(
            id=signal_id,
            market_id=cluster.market_id,
            outcome=(cluster.outcome or "YES"),
            signal_type=SignalType.MULTI_WALLET_CLUSTER,
            score=score.total,
            confidence=round(min(1.0, cluster.confidence / 100), 4),
            reason=f"{cluster.wallet_count} wallets traded {cluster.side} on {cluster.market_id} within window. Avg wallet score {cluster.average_wallet_score:.1f}.",
            action=decision.lower(),
            status=decision.lower(),
            decision=decision,
            wallets=None,
            cluster_id=cluster.id,
            proposed_size_usd=round(min(self._live_capital() * self.settings.paper_max_risk_per_trade, cluster.notional_usd * self._copy_pct()), 2),
            copyable_edge=score.copyable_edge / 100,
            notes=json.dumps(score.to_dict()),
        )
        self.session.merge(signal)
        self.session.commit()
        return self.session.get(Signal, signal_id) or signal

    def generate_signals_from_recent_audits(self, limit: int = 100) -> list[Signal]:
        statement = select(TradeAuditRecord).order_by(TradeAuditRecord.audited_at.desc()).limit(limit)
        audits = list(self.session.exec(statement).all())
        signals: list[Signal] = []
        for audit in audits:
            if audit.decision in ("WATCH", "SIGNAL", "PAPER_TRADE"):
                signals.append(self.build_signal_from_trade_audit(audit))
        return signals

    def generate_signals_from_recent_clusters(self, limit: int = 50) -> list[Signal]:
        statement = select(TradeCluster).order_by(TradeCluster.detected_at.desc()).limit(limit)
        clusters = list(self.session.exec(statement).all())
        return [self.build_cluster_signal(cluster) for cluster in clusters]

    # ---------------- internals ----------------

    def _signal_type_for_audit(self, audit: TradeAuditRecord) -> SignalType:
        if audit.wallet_tier in ("ELITE", "STRONG") and audit.notional_usd >= 50_000:
            return SignalType.WHALE_ENTRY
        if audit.wallet_tier in ("ELITE", "STRONG"):
            return SignalType.SMART_WALLET_ENTRY
        if (audit.side or "").upper() == "SELL":
            return SignalType.EXIT_ALERT
        if audit.copy_delay_seconds > 1_800:
            return SignalType.LATE_ENTRY_RISK
        return SignalType.WATCH_ONLY

    def _compute_audit_signal_score(self, audit: TradeAuditRecord) -> SignalScore:
        score = SignalScore()
        score.wallet_quality = round(audit.wallet_score * 0.30, 2)
        score.whale = round(min(15.0, audit.notional_usd / 25_000), 2)
        score.liquidity = round(min(15.0, audit.market_liquidity_score * 0.15), 2)
        score.spread = round(15.0 - min(15.0, (audit.estimated_spread or 0) * 200), 2)
        score.orderbook = round(_orderbook_quality_to_score(audit.orderbook_quality) * 0.10, 2)
        score.timing = round(max(0.0, 10.0 - min(10.0, audit.copy_delay_seconds / 600)), 2)
        score.copyable_edge = round(min(15.0, audit.copyable_edge * 300), 2)
        score.category_edge = 5.0
        score.confidence = round(min(10.0, audit.wallet_score / 10), 2)
        score.risk_penalty = 5.0 if audit.warnings and "spread_above_max" in (audit.warnings or "") else 0.0
        score.late_entry_penalty = 5.0 if audit.copy_delay_seconds > 1_800 else 0.0
        raw = (
            score.wallet_quality
            + score.whale
            + score.liquidity
            + score.spread
            + score.orderbook
            + score.timing
            + score.copyable_edge
            + score.category_edge
            + score.confidence
        )
        score.total = round(max(0.0, min(100.0, raw - score.risk_penalty - score.late_entry_penalty)), 2)
        return score

    def _compute_cluster_signal_score(self, cluster: TradeCluster) -> SignalScore:
        score = SignalScore()
        score.wallet_quality = round(min(35.0, cluster.average_wallet_score * 0.35), 2)
        score.cluster = round(min(20.0, cluster.wallet_count * 4), 2)
        score.whale = round(min(15.0, cluster.notional_usd / 25_000), 2)
        score.liquidity = 10.0
        score.spread = 10.0
        score.orderbook = 5.0
        score.timing = 5.0
        score.copyable_edge = round(min(10.0, cluster.confidence / 10), 2)
        score.confidence = round(min(10.0, cluster.confidence / 10), 2)
        raw = (
            score.wallet_quality
            + score.cluster
            + score.whale
            + score.liquidity
            + score.spread
            + score.orderbook
            + score.timing
            + score.copyable_edge
            + score.confidence
        )
        score.total = round(max(0.0, min(100.0, raw)), 2)
        return score

    def _derive_decision_from_audit(self, audit: TradeAuditRecord, total_score: float) -> str:
        if audit.wallet_tier in ("SUSPICIOUS", "INSUFFICIENT_DATA"):
            return "REJECT"
        if audit.copyable_edge <= 0:
            return "REJECT"
        if total_score < self.settings.min_signal_score:
            return "WATCH"
        if audit.orderbook_quality in ("UNTRADABLE", "BAD"):
            return "WATCH"
        if audit.estimated_spread and audit.estimated_spread > self.settings.max_spread_pct:
            return "WATCH"
        if audit.wallet_tier in ("ELITE", "STRONG") and audit.decision == "PAPER_TRADE":
            return "PAPER_TRADE"
        return "WATCH"

    def _reason_for_audit(self, audit: TradeAuditRecord, signal_type: SignalType) -> str:
        return (
            f"{signal_type.value}: wallet {audit.wallet_address} ({audit.wallet_tier}) "
            f"{audit.side} {audit.outcome} on {audit.market_id} "
            f"@ {audit.price} (notional ${audit.notional_usd:,.0f})."
        )

    def _propose_size(self, audit: TradeAuditRecord) -> float:
        live_capital = self._live_capital()
        max_risk = live_capital * self.settings.paper_max_risk_per_trade
        # 2026-05-17 test: à ELITE_OPEN+ tier ($10k+), bypass source cap → R-based fixed.
        # Permet de mesurer en paper si WR tient sur trades plus gros que source.
        # À tier inférieur, garde copy-faithful (= preserve baseline data).
        if live_capital >= 10000:
            # Fixed R-based sizing à haut tier
            return round(max_risk, 2)
        # Lower tiers : copy faithful proportionnel
        copy_pct = self._copy_pct()
        liquidity_cap = max(0.0, audit.notional_usd * copy_pct)
        return round(min(max_risk, liquidity_cap or max_risk), 2)

    def _live_capital(self) -> float:
        """Read live BotState.paper_capital (= effective capital after restart),
        fall back to static settings.paper_capital if unavailable. Single source
        of truth for sizing decisions in this engine.
        """
        try:
            from app.models.bot_state import BotState
            from sqlmodel import select
            bs = self.session.exec(select(BotState)).first()
            if bs and bs.paper_capital:
                return float(bs.paper_capital)
        except Exception:
            pass
        return float(self.settings.paper_capital)

    def _copy_pct(self) -> float:
        """2026-05-17 — copy_pct supprimé (per opérateur décision).
        Le bot copie la taille source à 100% à TOUS tiers. Le sizing final
        est borné en aval par R-based (capital_allocator), min_stake (preset),
        et exposure cap. Pas de cap source-proportional anti-front-running.

        Rationale : doctrine "même comportement paper/live" → pas de safeguard
        arbitraire qui s'active selon tier. Les caps existants (R, min_stake,
        exposure) sont suffisants et documentés.
        """
        return 1.00


def _orderbook_quality_to_score(quality: str) -> float:
    return {
        "EXCELLENT": 100.0,
        "GOOD": 80.0,
        "ACCEPTABLE": 60.0,
        "BAD": 30.0,
        "UNTRADABLE": 0.0,
        "INSUFFICIENT_DATA": 20.0,
    }.get(quality, 0.0)
