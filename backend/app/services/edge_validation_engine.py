from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.signal import Signal
from app.models.trade import NoTradeDecision, PaperTrade, TradeAuditRecord
from app.models.wallet import MarketFirstWalletRecord, WalletAudit
# v0.7.2: edge_validation_engine projects historical audits through several
# *capital scenarios* (analysis only, NOT runtime modes). The legacy named
# profiles (SAFE / AGGRESSIVE / FULL_PAPER) feed the same backtest filter but
# the output keys are neutralised to ``capital_scenario_{N}`` so the rest of
# the codebase stays free of mode-name references.
from app.services.risk_mode import (
    AGGRESSIVE as _LEGACY_AGGRESSIVE,
    FULL_PAPER as _LEGACY_FULL_PAPER,
    RiskModeProfile,
    SAFE as _LEGACY_SAFE,
)

EdgeConclusion = Literal["EDGE_CONFIRMED", "EDGE_WEAK", "EDGE_NOT_PROVEN", "EDGE_NEGATIVE", "INSUFFICIENT_DATA"]


@dataclass
class EdgeMetrics:
    copy_delay_seconds: float
    price_deterioration: float
    copyable_edge: float
    liquidity_adjusted_size: float
    smart_wallet_reliability: float
    category_edge: float
    naive_copy_baseline: float
    filtered_copy_strategy: float
    expectancy: float
    profit_factor: float
    max_drawdown: float
    average_trade_return: float
    slippage_impact: float
    spread_impact: float


class EdgeValidationEngine:
    def __init__(self, session: Session) -> None:
        self.session = session
        # 2026-05-07 leak fix: cache per-instance (one engine per request via
        # FastAPI Depends(get_session)). Without this cache, metrics() calls
        # closed_trades() 6+ times → 6× full table load (140k rows) per request.
        # Frontend hits /edge/report every 10s → MB/s leak.
        self._cached_trades: list[PaperTrade] | None = None
        self._cached_trade_audits: list[TradeAuditRecord] | None = None
        self._cached_wallet_audits: list[WalletAudit] | None = None
        self._cached_signals: list[Signal] | None = None
        self._cached_wallet_status_index: dict[str, dict[str, Any]] | None = None

    def trades(self) -> list[PaperTrade]:
        if self._cached_trades is not None:
            return self._cached_trades
        try:
            self._cached_trades = list(self.session.exec(select(PaperTrade)).all())
        except Exception:
            self._cached_trades = []
        return self._cached_trades

    def closed_trades(self) -> list[PaperTrade]:
        return [trade for trade in self.trades() if trade.status == "closed"]

    def compute_copy_delay_seconds(self) -> float:
        audits = self._trade_audits()
        if not audits:
            return 45.0
        return round(sum(audit.copy_delay_seconds for audit in audits) / len(audits), 2)

    def compute_price_deterioration(self) -> float:
        audits = self._trade_audits()
        if not audits:
            return 0.012
        return round(sum(audit.price_deterioration for audit in audits) / len(audits), 6)

    def compute_copyable_edge(self) -> float:
        audits = self._trade_audits()
        if not audits:
            return 0.0
        return round(sum(audit.copyable_edge for audit in audits) / len(audits), 6)

    def compute_liquidity_adjusted_size(self) -> float:
        audits = self._trade_audits()
        if not audits:
            return 100.0
        return round(sum(audit.notional_usd for audit in audits) / len(audits), 2)

    def compute_strategy_expectancy(self) -> float:
        closed = self.closed_trades()
        if not closed:
            return 0.0
        return round(sum(trade.realized_pnl for trade in closed) / len(closed), 4)

    def compute_profit_factor(self) -> float:
        closed = [trade.realized_pnl for trade in self.closed_trades()]
        gains = sum(pnl for pnl in closed if pnl > 0)
        losses = abs(sum(pnl for pnl in closed if pnl < 0))
        if not closed:
            return 0.0
        if losses == 0:
            return 99.0 if gains > 0 else 0.0
        return round(gains / losses, 3)

    def compute_max_drawdown(self) -> float:
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for trade in self.closed_trades():
            equity += trade.realized_pnl
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
        return round(abs(max_drawdown), 2)

    def compute_win_rate(self) -> float:
        closed = self.closed_trades()
        if not closed:
            return 0.0
        return round(len([trade for trade in closed if trade.realized_pnl > 0]) / len(closed), 3)

    def compute_average_win_loss(self) -> dict[str, float]:
        closed = [trade.realized_pnl for trade in self.closed_trades()]
        wins = [pnl for pnl in closed if pnl > 0]
        losses = [pnl for pnl in closed if pnl < 0]
        return {
            "average_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "average_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        }

    def compute_category_edge(self) -> float:
        audits = self._trade_audits()
        if not audits:
            return 0.0
        return round(sum(audit.copyable_edge for audit in audits if audit.copyable_edge > 0) / max(len(audits), 1), 6)

    def compute_wallet_reliability(self) -> float:
        wallets = self._wallet_audits()
        if not wallets:
            return 0.0
        return round(sum(wallet.reliability for wallet in wallets) / len(wallets), 2)

    def metrics(self) -> EdgeMetrics:
        return EdgeMetrics(
            copy_delay_seconds=self.compute_copy_delay_seconds(),
            price_deterioration=self.compute_price_deterioration(),
            copyable_edge=self.compute_copyable_edge(),
            liquidity_adjusted_size=self.compute_liquidity_adjusted_size(),
            smart_wallet_reliability=self.compute_wallet_reliability(),
            category_edge=self.compute_category_edge(),
            naive_copy_baseline=0.0,
            filtered_copy_strategy=self.compute_strategy_expectancy(),
            expectancy=self.compute_strategy_expectancy(),
            profit_factor=self.compute_profit_factor(),
            max_drawdown=self.compute_max_drawdown(),
            average_trade_return=self.compute_strategy_expectancy(),
            slippage_impact=0.005,
            spread_impact=0.01,
        )

    def conclusion(self) -> EdgeConclusion:
        closed_count = len(self.closed_trades())
        if closed_count < 30:
            return "INSUFFICIENT_DATA"
        if self.compute_strategy_expectancy() <= 0:
            return "EDGE_NEGATIVE"
        if self.compute_profit_factor() >= 1.3:
            return "EDGE_CONFIRMED"
        return "EDGE_WEAK"

    def compare_naive_copy_vs_filtered_copy(self) -> dict[str, float]:
        return {"naive_copy": 0.0, "filtered_copy": self.compute_strategy_expectancy()}

    # ---------------- v0.4 strategies + reporting ----------------

    def compare_strategies(self) -> dict[str, dict[str, float]]:
        audits = self._trade_audits()
        signals = self._signals()
        wallets = self._wallet_audits()
        wallet_status = self._wallet_status_index()
        return {
            "naive_copy": self._strategy_summary(audits, lambda audit: True),
            "filtered_smart_money": self._strategy_summary(
                audits,
                lambda audit: audit.wallet_tier in ("ELITE", "STRONG") and audit.copyable_edge > 0,
            ),
            "whale_only": self._strategy_summary(audits, lambda audit: audit.notional_usd >= 50_000),
            "consensus": self._consensus_summary(signals),
            "market_opportunity": {
                "trades": len(audits),
                "copyable_edge_avg": self.compute_copyable_edge(),
                "wallet_reliability_avg": (sum(wallet.reliability for wallet in wallets) / len(wallets)) if wallets else 0.0,
            },
            # v0.7.2: capital scenarios — three operating points the same
            # CapitalAllocator would emit at different bankroll sizes.
            "capital_scenario_50":   self._capital_scenario_strategy(audits, _LEGACY_SAFE,        wallet_status),
            "capital_scenario_500":  self._capital_scenario_strategy(audits, _LEGACY_AGGRESSIVE,  wallet_status),
            "capital_scenario_5000": self._capital_scenario_strategy(audits, _LEGACY_FULL_PAPER,  wallet_status),
        }

    def _wallet_status_index(self) -> dict[str, dict[str, Any]]:
        # 2026-05-07 leak fix: was loading ALL 239k MFWR per /edge/report hit.
        # Filter to addresses present in the audit window (max 1000 → ~hundreds unique).
        if self._cached_wallet_status_index is not None:
            return self._cached_wallet_status_index
        index: dict[str, dict[str, Any]] = {}
        addresses = {(a.wallet_address or "").lower() for a in self._trade_audits()}
        addresses.discard("")
        if not addresses:
            self._cached_wallet_status_index = index
            return index
        try:
            stmt = select(MarketFirstWalletRecord).where(
                func.lower(MarketFirstWalletRecord.address).in_(addresses)
            )
            records = list(self.session.exec(stmt).all())
        except Exception:
            self._cached_wallet_status_index = index
            return index
        for record in records:
            index[record.address.lower()] = {
                "candidate_status": record.candidate_status,
                "win_rate_confidence": record.win_rate_confidence,
                "resolved_markets_traded": record.resolved_markets_traded,
            }
        self._cached_wallet_status_index = index
        return index

    def _capital_scenario_strategy(
        self,
        audits: list[TradeAuditRecord],
        profile: RiskModeProfile,
        wallet_status: dict[str, dict[str, Any]],
    ) -> dict[str, float]:
        """Backtest projection for a capital scenario. v0.7.2: takes a legacy
        ``RiskModeProfile`` as a stand-in for the param shape the
        CapitalAllocator would use at the corresponding capital tier. Output
        keys are scenario-numeric ("capital_scenario_50" etc.) — no named
        modes leak into the API surface.
        """
        accepted: list[TradeAuditRecord] = []
        rejected = 0
        for audit in audits:
            wallet_id = (audit.wallet_address or "").lower()
            info = wallet_status.get(wallet_id, {})
            status = info.get("candidate_status")
            if status not in profile.allowed_statuses:
                rejected += 1
                continue
            if int(info.get("resolved_markets_traded") or 0) < profile.min_sample_size:
                rejected += 1
                continue
            if profile.require_medium_high_confidence and (info.get("win_rate_confidence") or "INSUFFICIENT_DATA") not in {"MEDIUM", "HIGH"}:
                rejected += 1
                continue
            if profile.require_copyable_edge and (audit.copyable_edge or 0) <= 0:
                rejected += 1
                continue
            if profile.require_orderbook_quality and audit.orderbook_quality in {"UNTRADABLE", "BAD", "INSUFFICIENT_DATA"}:
                rejected += 1
                continue
            if profile.require_spread_check and (audit.estimated_spread or 0) > 0.05:
                rejected += 1
                continue
            accepted.append(audit)
        if not accepted:
            return {
                "trades": 0,
                "rejected": rejected,
                "copyable_edge_avg": 0.0,
                "max_open_positions": float(profile.max_open_positions or 0),
                "max_total_exposure": profile.max_total_exposure,
                "no_daily_trade_count_limit": float(profile.no_daily_trade_count_limit),
            }
        return {
            "trades": len(accepted),
            "rejected": rejected,
            "copyable_edge_avg": round(sum(audit.copyable_edge for audit in accepted) / len(accepted), 6),
            "max_open_positions": float(profile.max_open_positions or 0),
            "max_total_exposure": profile.max_total_exposure,
            "no_daily_trade_count_limit": float(profile.no_daily_trade_count_limit),
        }

    def wallet_breakdown(self) -> list[dict[str, Any]]:
        audits = self._trade_audits()
        rows: dict[str, dict[str, Any]] = {}
        for audit in audits:
            row = rows.setdefault(audit.wallet_address, {"trades": 0, "edge": 0.0, "tier": audit.wallet_tier})
            row["trades"] += 1
            row["edge"] += audit.copyable_edge
        for row in rows.values():
            row["edge"] = round(row["edge"] / max(row["trades"], 1), 6)
        return [{"wallet": wallet, **data} for wallet, data in rows.items()]

    def category_breakdown(self) -> list[dict[str, Any]]:
        audits = self._trade_audits()
        rows: dict[str, dict[str, Any]] = {}
        for audit in audits:
            key = audit.outcome or "unknown"
            row = rows.setdefault(key, {"trades": 0, "edge": 0.0})
            row["trades"] += 1
            row["edge"] += audit.copyable_edge
        for row in rows.values():
            row["edge"] = round(row["edge"] / max(row["trades"], 1), 6)
        return [{"category": category, **data} for category, data in rows.items()]

    def no_trade_decision_log(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            statement = select(NoTradeDecision).order_by(NoTradeDecision.created_at.desc()).limit(limit)
            entries = list(self.session.exec(statement).all())
        except Exception:
            entries = []
        return [
            {
                "id": entry.id,
                "signal_id": entry.signal_id,
                "market_id": entry.market_id,
                "wallet_address": entry.wallet_address,
                "reason_code": entry.reason_code,
                "details": entry.details,
                "saved_loss_estimate": entry.saved_loss_estimate,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries
        ]

    def generate_edge_report(self) -> dict:
        metrics = self.metrics()
        return {
            "conclusion": self.conclusion(),
            "metrics": metrics.__dict__,
            "baseline": self.compare_naive_copy_vs_filtered_copy(),
            "strategies": self.compare_strategies(),
            "wallet_breakdown": self.wallet_breakdown(),
            "category_breakdown": self.category_breakdown(),
            "no_trade_decision_log": self.no_trade_decision_log(),
            "note": "Report based on local audits and paper trades. No live trading is derived from this report in v0.4.",
        }

    # ---------------- internals ----------------

    def _trade_audits(self) -> list[TradeAuditRecord]:
        # 2026-05-07 leak fix: cache + LIMIT to last 1000 records.
        # Old version loaded ALL tradeauditrecord (140k+ rows) per call,
        # called 4-5x in metrics() → 700k row loads per /edge/report.
        if self._cached_trade_audits is not None:
            return self._cached_trade_audits
        try:
            stmt = (
                select(TradeAuditRecord)
                .order_by(TradeAuditRecord.audited_at.desc())
                .limit(1000)
            )
            self._cached_trade_audits = list(self.session.exec(stmt).all())
        except Exception:
            self._cached_trade_audits = []
        return self._cached_trade_audits

    def _wallet_audits(self) -> list[WalletAudit]:
        if self._cached_wallet_audits is not None:
            return self._cached_wallet_audits
        try:
            self._cached_wallet_audits = list(self.session.exec(select(WalletAudit)).all())
        except Exception:
            self._cached_wallet_audits = []
        return self._cached_wallet_audits

    def _signals(self) -> list[Signal]:
        # 2026-05-07 leak fix: cache + LIMIT 1000 most recent. Was loading all
        # 138k Signal rows per call, called from compare_strategies on every
        # /edge/report hit (10s polling) → ~MB/s leak.
        if self._cached_signals is not None:
            return self._cached_signals
        try:
            stmt = select(Signal).order_by(Signal.created_at.desc()).limit(1000)
            self._cached_signals = list(self.session.exec(stmt).all())
        except Exception:
            self._cached_signals = []
        return self._cached_signals

    def _strategy_summary(self, audits: list[TradeAuditRecord], predicate) -> dict[str, float]:
        filtered = [audit for audit in audits if predicate(audit)]
        if not filtered:
            return {"trades": 0, "copyable_edge_avg": 0.0, "rejected": 0}
        avg_edge = sum(audit.copyable_edge for audit in filtered) / len(filtered)
        return {
            "trades": len(filtered),
            "copyable_edge_avg": round(avg_edge, 6),
            "rejected": len(audits) - len(filtered),
        }

    def _consensus_summary(self, signals: list[Signal]) -> dict[str, float]:
        consensus = [signal for signal in signals if signal.cluster_id]
        return {
            "trades": len(consensus),
            "average_score": round(sum(signal.score for signal in consensus) / len(consensus), 2) if consensus else 0.0,
            "paper_trade_decisions": len([signal for signal in consensus if signal.decision == "PAPER_TRADE"]),
        }
