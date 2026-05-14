"""CLOB retry queue service — 2026-05-14 review Round 9 strict-compatible fix.

Quand un audit ELITE strong-edge + tq>=70 + liq>0 est rejeté UNIQUEMENT pour
orderbook BAD/UNTRADABLE (CLOB 404 sur markets crypto 5min auto-générés), on
enqueue dans PendingClobRetry au lieu de rejeter définitivement. Drill
2026-05-14 montre que 42% des candidats récupèrent un CLOB book valide
quelques minutes après l'audit initial.

Phase 1 (cette version, clob_retry_auto_open_paper=False par défaut) :
- maybe_queue : crée PendingClobRetry si toutes les conditions strict alignées
- process_pending_batch : worker re-fetch CLOB toutes les 10-180s
  - Si CLOB OK + gates strict toujours pass → mark READY_TO_FILL (mesure uniquement)
  - Sinon → retry plus tard ou EXPIRED
- Aucun PaperTrade ouvert (mode collecte/metric pour validation runtime)

Phase 2 (clob_retry_auto_open_paper=True, à activer post-validation) :
- Worker ouvre directement le PaperTrade quand strict toujours OK → status FILLED_PAPER

Doctrine : aucun seuil baissé. On rejette toujours les vrais BAD. On corrige
juste un bug timing.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.models.market import Market
from app.models.trade import NoTradeDecision, PaperTrade, PendingClobRetry
from app.services.market_scanner import MarketScanner
from app.services.orderbook_analyzer import OrderbookAnalyzer

logger = logging.getLogger(__name__)


# Retry delays (seconds) by attempt count : 10, 30, 60, 120, 180
_RETRY_DELAYS_S = [10, 30, 60, 120, 180]


def _next_retry_delta(retry_count: int) -> timedelta:
    idx = min(retry_count, len(_RETRY_DELAYS_S) - 1)
    return timedelta(seconds=_RETRY_DELAYS_S[idx])


def _to_utc_aware(dt: datetime | None) -> datetime | None:
    """SQLite stores datetimes naive; normalize to UTC-aware for comparisons."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class ClobRetryService:
    """Strict-compatible retry queue for orderbook BAD/UNTRADABLE candidates.

    No threshold lowered. Re-tests CLOB book over time and only opens
    PaperTrade (or marks READY_TO_FILL in collect-only mode) when every gate
    still passes at retry time.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.market_scanner = MarketScanner(session)

    # ------------------------------------------------------------------ enqueue

    def maybe_queue(
        self,
        *,
        audit_id: str,
        wallet_address: str,
        market_id: str,
        outcome: str | None,
        side: str | None,
        raw_signal_price: float,
        notional_usd: float,
        copyable_edge: float,
        trade_quality_score: float,
        wallet_tier: str,
        orderbook_quality: str,
        market_liquidity_score: float,
    ) -> str | None:
        """Returns the new PendingClobRetry id if queued, else None.

        Strict conditions (all must hold):
        - clob_retry_enabled True AND paper mode (= not live, not strict-false)
        - wallet_tier == ELITE
        - copyable_edge >= 0.02 (STRONG_EDGE)
        - trade_quality_score >= 70
        - market_liquidity_score > 0
        - orderbook_quality in {BAD, UNTRADABLE}
        - source_trade_id not already queued (idempotent)
        """
        if not self.settings.clob_retry_enabled:
            return None
        if self.settings.live_enabled:
            return None
        if wallet_tier != "ELITE":
            return None
        if copyable_edge < 0.02:
            return None
        if trade_quality_score < 70:
            return None
        if market_liquidity_score <= 0:
            return None
        if orderbook_quality not in ("BAD", "UNTRADABLE"):
            return None

        # Idempotence
        existing = self.session.exec(
            select(PendingClobRetry).where(PendingClobRetry.source_trade_id == audit_id)
        ).first()
        if existing is not None:
            return None

        now = datetime.now(UTC)
        retry = PendingClobRetry(
            id=f"clobretry-{uuid.uuid4().hex[:16]}",
            source_trade_id=audit_id,
            wallet_address=wallet_address,
            market_id=market_id,
            outcome=outcome,
            side=side,
            raw_signal_price=raw_signal_price,
            notional_usd=notional_usd,
            copyable_edge=copyable_edge,
            trade_quality_score=trade_quality_score,
            wallet_tier=wallet_tier,
            first_seen_at=now,
            next_retry_at=now + _next_retry_delta(0),
            expires_at=now + timedelta(seconds=self.settings.clob_retry_max_age_seconds),
            retry_count=0,
            status="PENDING",
            last_orderbook_quality=orderbook_quality,
        )
        self.session.add(retry)
        # Traçabilité dans NoTradeDecision
        nt = NoTradeDecision(
            id=f"ntd-clobretry-queued-{uuid.uuid4().hex[:12]}",
            signal_id=None,
            market_id=market_id,
            wallet_address=wallet_address,
            reason_code="CLOB_RETRY_QUEUED",
            details=json.dumps({
                "audit_id": audit_id,
                "orderbook_quality": orderbook_quality,
                "copyable_edge": copyable_edge,
                "trade_quality_score": trade_quality_score,
                "expires_at": retry.expires_at.isoformat(),
            }),
        )
        self.session.add(nt)
        self.session.commit()
        return retry.id

    # ------------------------------------------------------------------ worker

    def process_pending_batch(self) -> dict[str, int]:
        """Process up to batch_size pending retries. Returns counts dict."""
        now = datetime.now(UTC)
        batch_size = max(1, int(self.settings.clob_retry_batch_size))
        rows = self.session.exec(
            select(PendingClobRetry)
            .where(PendingClobRetry.status == "PENDING")
            .where(PendingClobRetry.next_retry_at <= now)
            .order_by(PendingClobRetry.first_seen_at)
            .limit(batch_size)
        ).all()

        counts = {"checked": 0, "ready": 0, "retry": 0, "expired": 0, "error": 0, "filled_paper": 0}
        for pending in rows:
            counts["checked"] += 1
            try:
                # Expiration check first (normalize DB-stored naive datetimes)
                expires_at = _to_utc_aware(pending.expires_at)
                if expires_at is not None and now >= expires_at:
                    self._mark_expired(pending, now)
                    counts["expired"] += 1
                    continue

                outcome_status = self._recheck_one(pending, now)
                counts[outcome_status] = counts.get(outcome_status, 0) + 1
            except Exception as exc:
                pending.status = "ERROR"
                pending.last_error = f"{type(exc).__name__}: {exc}"
                pending.updated_at = now
                self.session.add(pending)
                self.session.commit()
                counts["error"] += 1
                logger.warning("clob_retry process error for %s: %s", pending.id, exc)
        return counts

    # ------------------------------------------------------------------ helpers

    def _mark_expired(self, pending: PendingClobRetry, now: datetime) -> None:
        pending.status = "EXPIRED"
        pending.updated_at = now
        self.session.add(pending)
        nt = NoTradeDecision(
            id=f"ntd-clobretry-expired-{uuid.uuid4().hex[:12]}",
            signal_id=None,
            market_id=pending.market_id,
            wallet_address=pending.wallet_address,
            reason_code="CLOB_RETRY_EXPIRED",
            details=json.dumps({
                "pending_id": pending.id,
                "source_trade_id": pending.source_trade_id,
                "retry_count": pending.retry_count,
                "last_orderbook_quality": pending.last_orderbook_quality,
            }),
        )
        self.session.add(nt)
        self.session.commit()

    def _recheck_one(self, pending: PendingClobRetry, now: datetime) -> str:
        """Re-fetch CLOB and decide. Returns one of: ready, filled_paper, retry, expired."""
        market = self.session.get(Market, pending.market_id) if pending.market_id else None
        if market is None:
            return self._reschedule_or_expire(pending, now, "no_market")

        # Re-fetch CLOB book using the same pattern as audit_engine
        # (_fetch_orderbook_summary_with_slippage) → ensures strict gate parity.
        token_ids: list[str] = []
        if market.clob_token_ids:
            try:
                parsed = json.loads(market.clob_token_ids)
                if isinstance(parsed, list):
                    token_ids = [str(t) for t in parsed if t]
            except (TypeError, ValueError):
                token_ids = []
        if not token_ids:
            return self._reschedule_or_expire(pending, now, "no_token_id")

        try:
            payload = self.market_scanner.fetch_orderbook(token_ids[0])
        except Exception as exc:
            pending.last_error = f"clob_fetch:{type(exc).__name__}"
            return self._reschedule_or_expire(pending, now, "clob_fetch_failed")

        analyzer = OrderbookAnalyzer(payload)
        summary = analyzer.summarize()
        pending.last_orderbook_quality = summary.quality
        pending.retry_count += 1
        pending.updated_at = now

        if summary.quality in ("GOOD", "ACCEPTABLE"):
            # Re-check liquidity score still > 0
            liq_score = self.market_scanner.compute_market_liquidity_score(market) if market else 0.0
            if liq_score <= 0:
                return self._reschedule_or_expire(pending, now, "liq_decay")

            # Spread guard (same as audit_engine)
            if summary.spread_pct is not None and summary.spread_pct > self.settings.max_spread_pct:
                return self._reschedule_or_expire(pending, now, "spread_high")

            # Tous les gates strict passent. Ouvrir paper (phase 2) ou mark READY (phase 1).
            if self.settings.clob_retry_auto_open_paper:
                pt_id = self._open_paper_trade(pending, summary, now)
                pending.status = "FILLED_PAPER"
                pending.last_error = f"paper_trade_id={pt_id}"
                self.session.add(pending)
                self._log_terminal(pending, "CLOB_RETRY_FILLED_PAPER", {"paper_trade_id": pt_id})
                self.session.commit()
                return "filled_paper"
            else:
                # Phase 1 collect-only : juste mesurer combien deviennent READY
                pending.status = "READY_TO_FILL"
                self.session.add(pending)
                self._log_terminal(pending, "CLOB_RETRY_READY_TO_FILL", {
                    "orderbook_quality": summary.quality,
                    "spread_pct": summary.spread_pct,
                    "liq_score": liq_score,
                })
                self.session.commit()
                return "ready"

        # Still BAD/UNTRADABLE → retry or expire
        return self._reschedule_or_expire(pending, now, "still_bad")

    def _reschedule_or_expire(self, pending: PendingClobRetry, now: datetime, why: str) -> str:
        max_attempts = max(1, int(self.settings.clob_retry_max_attempts))
        if pending.retry_count >= max_attempts:
            pending.status = "EXPIRED"
            pending.last_error = f"max_attempts:{why}"
            self.session.add(pending)
            self._log_terminal(pending, "CLOB_RETRY_EXPIRED", {"why": why})
            self.session.commit()
            return "expired"
        pending.next_retry_at = now + _next_retry_delta(pending.retry_count)
        pending.last_error = why
        self.session.add(pending)
        self.session.commit()
        return "retry"

    def _log_terminal(self, pending: PendingClobRetry, reason_code: str, extra: dict[str, Any]) -> None:
        nt = NoTradeDecision(
            id=f"ntd-{reason_code.lower()}-{uuid.uuid4().hex[:12]}",
            signal_id=None,
            market_id=pending.market_id,
            wallet_address=pending.wallet_address,
            reason_code=reason_code,
            details=json.dumps({
                "pending_id": pending.id,
                "source_trade_id": pending.source_trade_id,
                "retry_count": pending.retry_count,
                "last_orderbook_quality": pending.last_orderbook_quality,
                **extra,
            }),
        )
        self.session.add(nt)

    def _open_paper_trade(self, pending: PendingClobRetry, summary, now: datetime) -> str:
        """Open a paper trade from a pending retry. Phase 2 only.

        Uses raw_signal_price (= source wallet entry) as average_price reference.
        notional_usd is preserved. signal_id None (retry-originated, not signal).
        """
        # Compute quantity from notional and current best ask if available, else from raw_signal_price
        ref_price = (
            summary.midpoint
            if getattr(summary, "midpoint", None) and summary.midpoint > 0
            else pending.raw_signal_price
        ) or pending.raw_signal_price
        if ref_price <= 0:
            ref_price = pending.raw_signal_price
        qty = pending.notional_usd / ref_price if ref_price > 0 else 0.0

        pt = PaperTrade(
            id=f"papertrade-clobretry-{uuid.uuid4().hex[:16]}",
            market_id=pending.market_id,
            outcome=pending.outcome or "",
            side=pending.side or "BUY",
            quantity=round(qty, 6),
            average_price=round(ref_price, 6),
            notional_usd=round(pending.notional_usd, 4),
            wallet_address=pending.wallet_address,
            signal_id=None,
            auto=True,
            status="open",
            opened_at=now,
        )
        self.session.add(pt)
        return pt.id
