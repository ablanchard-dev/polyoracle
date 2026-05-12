"""D3 state recovery au boot (Phase D — 2026-05-11).

Au boot du backend, audit toutes les PaperTrade status='open' et :
1. Logs un récapitulatif (count, oldest, total exposure)
2. Vérifie qu'aucune n'est orpheline (close_reason=None + opened_at très ancien)
3. Réintègre dans le close-loop scheduler (via AdaptiveCloseScheduler)
4. Force un audit de résolution immédiat (au cas où markets résolus pendant downtime)

Mode initial = audit-only (pas de modification DB). Activation runtime nécessite :
- Hook dans main.py @app.on_event("startup")
- Wiring vers AdaptiveCloseScheduler.enqueue_existing_positions()

Aujourd'hui (Phase A complement) : skeleton + tests. Pas activé runtime tant
que main.py pas modifié (évite restart backend en plein smoke).

Critère go/no-go (D3):
  - kill -9 backend pendant 12 positions ouvertes
  - restart auto via systemd (D1)
  - state_recovery_audit_at_boot() loggue 12 positions reprises
  - close-loop reprend les 12 sans duplication
  - aucune position perdue, aucune duplicata créée
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app.models.trade import PaperTrade

# --------- Thresholds ---------

ORPHAN_AGE_HOURS_THRESHOLD = 168.0  # > 7 jours sans close = suspect orphan
STALE_AGE_HOURS_THRESHOLD = 24.0  # > 24h sans close mais pas orphan
EMERGENCY_CLOSE_REASON = "EMERGENCY_RECOVERY_BOOT"


@dataclass
class RecoveryAudit:
    open_positions_count: int
    total_open_notional: float
    oldest_position_age_hours: float | None
    stale_count: int  # ≥24h
    orphan_count: int  # ≥7d
    by_market_id: dict[str, int]  # market_id → count
    recommendations: list[str]
    issued_at: str


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def audit_open_positions_at_boot(session: Session) -> RecoveryAudit:
    """Pure audit — read-only. Returns a snapshot + actionable recommendations.
    Caller (main.py @startup) decides what to do (log, alert, force close, etc.).
    """
    now = datetime.now(UTC)
    open_trades = list(session.exec(
        select(PaperTrade).where(PaperTrade.status == "open")
    ).all())

    total_notional = sum(float(t.notional_usd or 0) for t in open_trades)
    oldest_age: float | None = None
    stale = 0
    orphan = 0
    by_market: dict[str, int] = {}

    for t in open_trades:
        opened = _ensure_aware(t.opened_at)
        if opened is not None:
            age_h = (now - opened).total_seconds() / 3600
            if oldest_age is None or age_h > oldest_age:
                oldest_age = age_h
            if age_h >= ORPHAN_AGE_HOURS_THRESHOLD:
                orphan += 1
            elif age_h >= STALE_AGE_HOURS_THRESHOLD:
                stale += 1
        by_market[t.market_id or "?"] = by_market.get(t.market_id or "?", 0) + 1

    recs: list[str] = []
    if orphan:
        recs.append(
            f"⚠ {orphan} positions ≥ {ORPHAN_AGE_HOURS_THRESHOLD/24:.0f}j — "
            "possibly orphaned. Run audit + manual close if confirmed."
        )
    if stale:
        recs.append(
            f"{stale} positions stale (24h+). Verify polling actually picks up "
            "their markets, not stuck in close-loop."
        )
    if total_notional > 100:  # NANO baseline
        recs.append(
            f"total open notional ${total_notional:.2f} > 100 — "
            "consider verifying capital allocator state at boot."
        )
    if not open_trades:
        recs.append("No open positions — clean boot.")

    return RecoveryAudit(
        open_positions_count=len(open_trades),
        total_open_notional=round(total_notional, 4),
        oldest_position_age_hours=round(oldest_age, 2) if oldest_age else None,
        stale_count=stale,
        orphan_count=orphan,
        by_market_id=by_market,
        recommendations=recs,
        issued_at=now.isoformat(timespec="seconds"),
    )


def reschedule_open_positions_into_closeloop(
    session: Session,
    *,
    scheduler: Any | None = None,  # AdaptiveCloseScheduler instance — typed Any to avoid cycles
) -> int:
    """Re-enqueue all open paper positions into the close-loop scheduler.

    If scheduler is None, only counts (audit). Otherwise calls
    scheduler.enqueue(trade) for each.

    Returns count of positions re-enqueued.
    """
    open_trades = list(session.exec(
        select(PaperTrade).where(PaperTrade.status == "open")
    ).all())
    n = 0
    for t in open_trades:
        if scheduler is not None and hasattr(scheduler, "enqueue"):
            try:
                scheduler.enqueue(t)
                n += 1
            except Exception:
                # Best-effort — caller will log
                continue
        else:
            n += 1
    return n


def emergency_close_orphans(
    session: Session, *, fair_exit_price: float | None = None,
) -> int:
    """Last-resort: close all positions with age ≥ ORPHAN_AGE_HOURS_THRESHOLD
    at fair_exit_price (or PaperTrade.average_price if None — preserves
    notional, no PnL impact).

    Use ONLY after operator confirmation — destructive (modifies trade state).
    Returns count of closed positions.
    """
    now = datetime.now(UTC)
    threshold = now - timedelta(hours=ORPHAN_AGE_HOURS_THRESHOLD)
    candidates = list(session.exec(
        select(PaperTrade)
        .where(PaperTrade.status == "open")
        .where(PaperTrade.opened_at < threshold)
    ).all())
    n = 0
    for t in candidates:
        exit_price = fair_exit_price if fair_exit_price is not None else float(t.average_price or 0)
        t.status = "closed"
        t.closed_at = now
        t.realized_pnl = 0.0  # neutral exit at entry price
        t.close_reason = EMERGENCY_CLOSE_REASON
        session.add(t)
        n += 1
    if n:
        session.commit()
    return n


def recovery_payload(session: Session) -> dict[str, Any]:
    """Convenience for /bot/recovery-audit endpoint."""
    return asdict(audit_open_positions_at_boot(session))
