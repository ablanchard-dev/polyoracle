from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from app.config import get_settings
from app.models.market import Market
from app.models.trade import (
    NoTradeDecision,
    PublicTrade,
    SmartMoneyEvent,
    TradeAuditRecord,
    TradeCluster,
)
from app.models.wallet import MarketFirstWalletRecord, Wallet
from app.services.copyable_edge_engine import CopyableEdgeEngine, EdgeBreakdown
from app.services.market_scanner import MarketScanner
from app.services.orderbook_analyzer import OrderbookAnalyzer, OrderbookSummary
from app.services.smart_wallet_auditor import SmartWalletAuditor, _normalize_address, _parse_dt, _safe_float

logger = logging.getLogger(__name__)


TRADE_DECISIONS = ("IGNORE", "WATCH", "SIGNAL", "PAPER_TRADE")


@dataclass
class TradeAuditResult:
    trade_id: str
    timestamp: datetime
    wallet: str
    market_id: str
    question: str | None
    outcome: str | None
    side: str | None
    price: float
    size: float
    notional_usd: float
    estimated_spread: float
    estimated_slippage: float
    wallet_score: float
    wallet_tier: str
    market_liquidity_score: float
    orderbook_quality: str
    copy_delay_seconds: float
    price_deterioration: float
    copyable_edge: float
    trade_quality_score: float
    decision: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    edge_breakdown: EdgeBreakdown | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "timestamp": self.timestamp.isoformat(),
            "wallet": self.wallet,
            "market_id": self.market_id,
            "question": self.question,
            "outcome": self.outcome,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "notional_usd": self.notional_usd,
            "estimated_spread": self.estimated_spread,
            "estimated_slippage": self.estimated_slippage,
            "wallet_score": self.wallet_score,
            "wallet_tier": self.wallet_tier,
            "market_liquidity_score": self.market_liquidity_score,
            "orderbook_quality": self.orderbook_quality,
            "copy_delay_seconds": self.copy_delay_seconds,
            "price_deterioration": self.price_deterioration,
            "copyable_edge": self.copyable_edge,
            "trade_quality_score": self.trade_quality_score,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "edge_breakdown": self.edge_breakdown.to_dict() if self.edge_breakdown else None,
        }


class TradeAuditEngine:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.auditor = SmartWalletAuditor(session)
        self.market_scanner = MarketScanner(session)
        self.edge_engine = CopyableEdgeEngine()

    # ---------------- ingestion ----------------

    def fetch_recent_public_trades(self, limit: int | None = None) -> list[dict[str, Any]]:
        cap = limit or self.settings.max_trades_per_cycle
        trades: list[dict[str, Any]] = []
        try:
            for entry in self.auditor.discover_top_wallets(limit=self.settings.top_wallets_min):
                address = entry.get("address")
                if not address:
                    continue
                wallet_trades = self.auditor.fetch_wallet_recent_trades(address, limit=20)
                trades.extend(wallet_trades)
                if len(trades) >= cap:
                    break
        except Exception as exc:
            logger.warning("Recent public trades fetch failed: %s", exc)
        return trades[:cap]

    def fetch_market_trades(self, market_id: str) -> list[dict[str, Any]]:
        return [trade for trade in self.fetch_recent_public_trades() if trade.get("market_id") == market_id]

    def fetch_wallet_trades(self, wallet: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self.auditor.fetch_wallet_recent_trades(wallet, limit=limit)

    def normalize_trade(self, raw: dict[str, Any]) -> dict[str, Any]:
        traded_at = raw.get("traded_at")
        if not isinstance(traded_at, datetime):
            traded_at = _parse_dt(traded_at) or datetime.now(UTC)
        return {
            "trade_id": str(raw.get("trade_id") or raw.get("id") or uuid4()),
            "wallet": _normalize_address(raw.get("address") or raw.get("wallet") or raw.get("user")),
            "market_id": str(raw.get("market_id") or raw.get("market") or ""),
            "outcome": raw.get("outcome"),
            "side": (raw.get("side") or "").upper() or None,
            "price": _safe_float(raw.get("price")),
            "size": _safe_float(raw.get("size")),
            "notional_usd": _safe_float(raw.get("notional_usd")) or _safe_float(raw.get("price")) * _safe_float(raw.get("size")),
            "traded_at": traded_at,
            "data_source": raw.get("data_source") or "polymarket_data",
        }

    def save_trade(self, normalized: dict[str, Any]) -> PublicTrade:
        record = PublicTrade(
            id=normalized["trade_id"],
            wallet_address=normalized["wallet"] or "unknown",
            market_id=normalized["market_id"] or "unknown",
            outcome=normalized.get("outcome"),
            side=normalized.get("side"),
            price=normalized["price"],
            size=normalized["size"],
            notional_usd=normalized["notional_usd"],
            traded_at=normalized.get("traded_at"),
            data_source=normalized["data_source"],
        )
        self.session.merge(record)
        self.session.commit()
        return record

    # ---------------- detection helpers ----------------

    def detect_large_trade(self, trade: dict[str, Any], threshold_usd: float = 25_000) -> bool:
        return _safe_float(trade.get("notional_usd")) >= threshold_usd

    def detect_repeated_accumulation(self, wallet: str, market_id: str, side: str, window_hours: int = 6) -> int:
        statement = (
            select(PublicTrade)
            .where(PublicTrade.wallet_address == wallet)
            .where(PublicTrade.market_id == market_id)
            .where(PublicTrade.side == side)
        )
        trades = list(self.session.exec(statement).all())
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        return len([trade for trade in trades if trade.traded_at and trade.traded_at >= cutoff])

    def detect_wallet_entry(self, wallet: str, market_id: str) -> bool:
        statement = (
            select(PublicTrade)
            .where(PublicTrade.wallet_address == wallet)
            .where(PublicTrade.market_id == market_id)
        )
        trades = list(self.session.exec(statement).all())
        return len(trades) <= 1

    def detect_wallet_exit(self, wallet: str, market_id: str) -> bool:
        statement = (
            select(PublicTrade)
            .where(PublicTrade.wallet_address == wallet)
            .where(PublicTrade.market_id == market_id)
            .order_by(PublicTrade.traded_at.desc())
            .limit(1)
        )
        last = self.session.exec(statement).first()
        return bool(last and (last.side or "").upper() == "SELL")

    def detect_trade_cluster(self, market_id: str, side: str, window_minutes: int = 60) -> TradeCluster | None:
        cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
        statement = (
            select(PublicTrade)
            .where(PublicTrade.market_id == market_id)
            .where(PublicTrade.side == side)
        )
        trades = [trade for trade in self.session.exec(statement).all() if trade.traded_at and trade.traded_at >= cutoff]
        if not trades:
            return None
        wallets = {trade.wallet_address for trade in trades if trade.wallet_address}
        if len(wallets) < 3:
            return None
        notional = sum(trade.notional_usd for trade in trades)
        avg_price = sum(trade.price for trade in trades) / len(trades)
        wallet_scores = []
        for address in wallets:
            wallet = self.session.get(Wallet, address)
            if wallet:
                wallet_scores.append(wallet.score)
        avg_score = sum(wallet_scores) / len(wallet_scores) if wallet_scores else 0.0
        cluster = TradeCluster(
            id=str(uuid4()),
            market_id=market_id,
            outcome=trades[0].outcome,
            side=side,
            wallet_count=len(wallets),
            trade_count=len(trades),
            notional_usd=round(notional, 2),
            average_price=round(avg_price, 4),
            average_wallet_score=round(avg_score, 2),
            started_at=min(trade.traded_at for trade in trades if trade.traded_at),
            ended_at=max(trade.traded_at for trade in trades if trade.traded_at),
            confidence=round(min(100.0, avg_score * 0.5 + len(wallets) * 5), 2),
        )
        self.session.add(cluster)
        self.session.commit()
        return cluster

    def detect_smart_money_move(self, audit: TradeAuditResult) -> SmartMoneyEvent | None:
        if audit.wallet_tier not in ("ELITE", "STRONG") or audit.notional_usd < 5_000:
            return None
        event = SmartMoneyEvent(
            id=str(uuid4()),
            event_type="SMART_WALLET_MOVE",
            wallet_address=audit.wallet,
            market_id=audit.market_id,
            outcome=audit.outcome,
            notional_usd=audit.notional_usd,
            price=audit.price,
            confidence=round((audit.wallet_score + audit.trade_quality_score) / 2, 2),
            summary=f"{audit.wallet_tier} wallet {audit.wallet} {audit.side} {audit.outcome} @ {audit.price}",
        )
        self.session.add(event)
        self.session.commit()
        return event

    # ---------------- audit pipeline ----------------

    def audit_trade(self, raw: dict[str, Any]) -> TradeAuditResult:
        normalized = self.normalize_trade(raw)
        wallet = normalized["wallet"]
        market_id = normalized["market_id"]
        traded_at = normalized["traded_at"]
        side = normalized["side"] or "BUY"
        warnings: list[str] = []
        reasons: list[str] = []

        wallet_record = self.session.get(Wallet, wallet) if wallet else None
        # v0.7.8 P6 fix (B14): MFWR is the source of truth for wallet tier
        # AND score. Legacy Wallet.tier/score is stale for 620/621 ELITE
        # wallets. MFWR.composite_score replaces Wallet.score.
        _mfwr = self.session.get(MarketFirstWalletRecord, wallet) if wallet else None
        wallet_tier = (
            _mfwr.candidate_status
            if _mfwr and _mfwr.candidate_status
            else (wallet_record.tier if wallet_record else "INSUFFICIENT_DATA")
        )
        wallet_score = (
            _mfwr.composite_score
            if _mfwr and _mfwr.composite_score is not None
            else (wallet_record.score if wallet_record else 0.0)
        )

        market_record = self.session.get(Market, market_id) if market_id else None
        if market_record is None:
            try:
                market_record = self.market_scanner.fetch_market_details(market_id)
            except Exception as exc:
                logger.warning("Market detail fetch failed for %s: %s", market_id, exc)
                market_record = None
        question = market_record.question if market_record else None

        orderbook_summary, slippage = self._fetch_orderbook_summary_with_slippage(market_record, normalized["notional_usd"])
        market_liquidity_score = self.market_scanner.compute_market_liquidity_score(market_record) if market_record else 0.0

        copy_delay = self.edge_engine.compute_copy_delay(traded_at)
        current_price = orderbook_summary.midpoint or normalized["price"]
        breakdown = self.edge_engine.compute_copyable_edge(
            wallet_score=wallet_score,
            signal_score=wallet_score,
            spread_pct=orderbook_summary.spread_pct,
            slippage=slippage,
            copy_delay_seconds=copy_delay,
            original_price=normalized["price"],
            current_price=current_price,
            side=side,
        )

        trade_quality_score = round(
            min(100.0,
                wallet_score * 0.4
                + market_liquidity_score * 0.2
                + (100 - min(100, (orderbook_summary.spread_pct or 0.05) * 2_000)) * 0.2
                + breakdown.score * 0.2),
            2,
        )

        if wallet_score >= 80:
            reasons.append("wallet_score_high")
        if self.detect_large_trade(normalized):
            reasons.append("large_trade")
        if orderbook_summary.quality in ("EXCELLENT", "GOOD"):
            reasons.append(f"orderbook_{orderbook_summary.quality.lower()}")
        if breakdown.classification == "STRONG_EDGE":
            reasons.append("copyable_edge_strong")

        if orderbook_summary.quality == "INSUFFICIENT_DATA":
            warnings.append("orderbook_unknown")
        if orderbook_summary.spread_pct and orderbook_summary.spread_pct > self.settings.max_spread_pct:
            warnings.append("spread_above_max")
        if copy_delay > 1_800:
            warnings.append("copy_delay_high")
        if breakdown.classification in ("NO_EDGE", "NEGATIVE_EDGE"):
            warnings.append(f"edge_{breakdown.classification.lower()}")
        if wallet_tier in ("SUSPICIOUS", "INSUFFICIENT_DATA"):
            warnings.append(f"wallet_tier_{wallet_tier.lower()}")

        decision = self._decide(
            wallet_tier=wallet_tier,
            wallet_score=wallet_score,
            quality=orderbook_summary.quality,
            spread_pct=orderbook_summary.spread_pct,
            edge=breakdown,
            trade_quality_score=trade_quality_score,
            market_liquidity_score=market_liquidity_score,
        )

        result = TradeAuditResult(
            trade_id=normalized["trade_id"],
            timestamp=traded_at,
            wallet=wallet or "unknown",
            market_id=market_id or "unknown",
            question=question,
            outcome=normalized.get("outcome"),
            side=side,
            price=normalized["price"],
            size=normalized["size"],
            notional_usd=normalized["notional_usd"],
            estimated_spread=orderbook_summary.spread or 0.0,
            estimated_slippage=slippage,
            wallet_score=wallet_score,
            wallet_tier=wallet_tier,
            market_liquidity_score=market_liquidity_score,
            orderbook_quality=orderbook_summary.quality,
            copy_delay_seconds=copy_delay,
            price_deterioration=breakdown.price_deterioration,
            copyable_edge=breakdown.copyable_edge,
            trade_quality_score=trade_quality_score,
            decision=decision,
            reasons=reasons,
            warnings=warnings,
            edge_breakdown=breakdown,
        )

        self.save_trade(normalized)
        self._persist_audit(result)
        self.detect_smart_money_move(result)
        return result

    def audit_recent_public_trades(self, limit: int | None = None) -> list[TradeAuditResult]:
        results: list[TradeAuditResult] = []
        for raw in self.fetch_recent_public_trades(limit=limit):
            try:
                results.append(self.audit_trade(raw))
            except Exception as exc:
                logger.warning("Trade audit failed: %s", exc)
        return results

    def compute_trade_quality(self, audit: TradeAuditResult) -> float:
        return audit.trade_quality_score

    def compute_trade_copyability(self, audit: TradeAuditResult) -> float:
        if audit.edge_breakdown is None:
            return 0.0
        return round(audit.edge_breakdown.score, 2)

    def compute_trade_outcome_if_available(self, audit: TradeAuditResult) -> dict[str, Any]:
        market = self.session.get(Market, audit.market_id) if audit.market_id else None
        if market is None or market.yes_price is None:
            return {"resolved": False, "pnl": None}
        current = market.yes_price if (audit.outcome or "").upper() == "YES" else market.no_price
        if current is None:
            return {"resolved": False, "pnl": None}
        pnl = (current - audit.price) * audit.size if (audit.side or "").upper() == "BUY" else (audit.price - current) * audit.size
        return {"resolved": True, "pnl": round(pnl, 2)}

    def list_audited_trades(self, limit: int = 200) -> list[TradeAuditRecord]:
        statement = select(TradeAuditRecord).order_by(TradeAuditRecord.audited_at.desc()).limit(limit)
        return list(self.session.exec(statement).all())

    def list_clusters(self, limit: int = 50) -> list[TradeCluster]:
        statement = select(TradeCluster).order_by(TradeCluster.detected_at.desc()).limit(limit)
        return list(self.session.exec(statement).all())

    def list_smart_money_events(self, limit: int = 100) -> list[SmartMoneyEvent]:
        statement = select(SmartMoneyEvent).order_by(SmartMoneyEvent.created_at.desc()).limit(limit)
        return list(self.session.exec(statement).all())

    def list_large_trades(self, limit: int = 100, threshold_usd: float = 25_000) -> list[TradeAuditRecord]:
        statement = (
            select(TradeAuditRecord)
            .where(TradeAuditRecord.notional_usd >= threshold_usd)
            .order_by(TradeAuditRecord.audited_at.desc())
            .limit(limit)
        )
        return list(self.session.exec(statement).all())

    def list_recent_public_trades(self, limit: int = 200) -> list[PublicTrade]:
        statement = select(PublicTrade).order_by(PublicTrade.seen_at.desc()).limit(limit)
        return list(self.session.exec(statement).all())

    # ---------------- internals ----------------

    def _fetch_orderbook_summary_with_slippage(self, market: Market | None, notional_usd: float) -> tuple[OrderbookSummary, float]:
        if market is None:
            return OrderbookSummary(), 0.0
        if not market.clob_token_ids:
            return self._mock_orderbook_summary(market, notional_usd)
        try:
            token_ids = json.loads(market.clob_token_ids)
        except (TypeError, ValueError):
            token_ids = []
        if not token_ids:
            return self._mock_orderbook_summary(market, notional_usd)
        token_id = str(token_ids[0])
        try:
            payload = self.market_scanner.fetch_orderbook(token_id)
            analyzer = OrderbookAnalyzer(payload)
            summary = analyzer.summarize()
            slippage = analyzer.estimate_slippage_for_size(notional_usd) if notional_usd > 0 else (summary.spread_pct or 0.0)
            if summary.quality == "INSUFFICIENT_DATA":
                return self._mock_orderbook_summary(market, notional_usd)
            return summary, slippage
        except Exception as exc:
            logger.warning("Orderbook fetch failed for %s: %s", token_id, exc)
            return self._mock_orderbook_summary(market, notional_usd)

    def _mock_orderbook_summary(self, market: Market, notional_usd: float) -> tuple[OrderbookSummary, float]:
        # When real CLOB data is unavailable we synthesize a conservative orderbook
        # from the market's spread/liquidity so downstream filters still get a quality estimate.
        spread = float(market.spread or 0.02)
        midpoint = float(market.yes_price or 0.5)
        liquidity = float(market.liquidity or 0.0)
        if liquidity <= 0:
            return OrderbookSummary(token_id=None), 0.0
        size_per_side = liquidity / 2 / max(midpoint, 0.01)
        synthetic = {
            "token_id": None,
            "orderbook": {
                "bids": [{"price": max(0.01, midpoint - spread / 2), "size": size_per_side}],
                "asks": [{"price": min(0.99, midpoint + spread / 2), "size": size_per_side}],
            },
            "midpoint": {"mid": midpoint},
        }
        analyzer = OrderbookAnalyzer(synthetic)
        summary = analyzer.summarize()
        summary.notes.append("synthetic_from_market_liquidity")
        slippage = analyzer.estimate_slippage_for_size(notional_usd) if notional_usd > 0 else (summary.spread_pct or 0.0)
        return summary, slippage

    def _decide(
        self,
        wallet_tier: str,
        wallet_score: float,
        quality: str,
        spread_pct: float | None,
        edge: EdgeBreakdown,
        trade_quality_score: float,
        market_liquidity_score: float,
    ) -> str:
        if wallet_tier == "SUSPICIOUS" or wallet_tier == "INSUFFICIENT_DATA":
            return "IGNORE"
        if edge.classification in ("NO_EDGE", "NEGATIVE_EDGE"):
            return "WATCH" if wallet_tier in ("ELITE", "STRONG") else "IGNORE"
        if quality in ("UNTRADABLE", "BAD"):
            return "WATCH"
        if spread_pct is not None and spread_pct > self.settings.max_spread_pct:
            return "WATCH"
        if market_liquidity_score < self.settings.min_liquidity_score:
            return "WATCH"
        if wallet_tier in ("ELITE", "STRONG") and trade_quality_score >= 70 and edge.classification == "STRONG_EDGE":
            return "PAPER_TRADE"
        if wallet_score >= 70 and trade_quality_score >= 60 and edge.classification != "NO_EDGE":
            return "SIGNAL"
        return "WATCH"

    def _persist_audit(self, audit: TradeAuditResult) -> None:
        record = TradeAuditRecord(
            id=f"audit-{audit.trade_id}",
            trade_id=audit.trade_id,
            wallet_address=audit.wallet,
            market_id=audit.market_id,
            question=audit.question,
            outcome=audit.outcome,
            side=audit.side,
            price=audit.price,
            size=audit.size,
            notional_usd=audit.notional_usd,
            estimated_spread=audit.estimated_spread,
            estimated_slippage=audit.estimated_slippage,
            wallet_score=audit.wallet_score,
            wallet_tier=audit.wallet_tier,
            market_liquidity_score=audit.market_liquidity_score,
            orderbook_quality=audit.orderbook_quality,
            copy_delay_seconds=audit.copy_delay_seconds,
            price_deterioration=audit.price_deterioration,
            copyable_edge=audit.copyable_edge,
            trade_quality_score=audit.trade_quality_score,
            decision=audit.decision,
            reasons=json.dumps(audit.reasons) if audit.reasons else None,
            warnings=json.dumps(audit.warnings) if audit.warnings else None,
        )
        self.session.merge(record)
        self.session.commit()

    def stats(self) -> dict[str, Any]:
        audits = list(self.session.exec(select(TradeAuditRecord)).all())
        decisions: dict[str, int] = defaultdict(int)
        for audit in audits:
            decisions[audit.decision] += 1
        return {
            "audited_count": len(audits),
            "decision_breakdown": dict(decisions),
            "clusters": len(self.list_clusters(limit=10_000)),
            "smart_money_events": len(self.list_smart_money_events(limit=10_000)),
        }

    def list_no_trade_decisions(self, limit: int = 200) -> list[NoTradeDecision]:
        statement = select(NoTradeDecision).order_by(NoTradeDecision.created_at.desc()).limit(limit)
        return list(self.session.exec(statement).all())
