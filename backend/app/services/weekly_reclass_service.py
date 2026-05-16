"""v0.7.8 Phase 5 — Weekly reclass service.

The bot needs the ELITE cohort to evolve over time:
  - new wallets that meet ELITE criteria → promote
  - wallets that lost their edge → demote (DROPPED / OUTLIER_FLAGGED)
  - data quality drift detection

This service runs once per week (Sunday 02:00 UTC) and:
  1. Backs up current state (DB + cohort CSV)
  2. Reads MFWR rows updated since last run
  3. Applies promotion/demotion rules with STRICT criteria (no relax)
  4. Writes audit trail (`walletreclassentry` events)
  5. Reloads polling cohort
  6. Notifies on significant changes (>10% cohort delta)

Hard rules:
  - Cohort never empty (fallback to previous cohort if reclass yields 0)
  - DB backup before any write
  - Idempotent: running twice in a row produces same result (no double-promote)
  - Failure recovery: if reclass crashes, bot continues with previous cohort
  - No relax of quality thresholds — same ELITE criteria as initial classification
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

logger = logging.getLogger(__name__)


# v0.7.8 P6 — Operator-aligned classification thresholds.
#
# Single source of truth: a wallet is ELITE iff
# (resolved_markets >= 100) AND (resolved_market_win_rate >= 0.90).
# A wallet is STRONG iff (resolved_markets >= 70) AND (0.70 <= win_rate < 0.90).
# Anything below STRONG is filtered out of the tradable cohort entirely.
#
# composite_score is intentionally NOT a gate here — production data shows
# it's stuck at exactly 88.0 for ~99% of the cohort (stuck-cap bug, not a
# discriminating signal). Using it would either pass everything or block
# everything. Win rate × sample size is the operator's actual contract.

# ELITE — top tier
ELITE_MIN_RESOLVED_MARKETS: int = 100
ELITE_MIN_WIN_RATE: float = 0.90

# STRONG — second tier (also tradable at all capital levels)
STRONG_MIN_RESOLVED_MARKETS: int = 70
STRONG_MIN_WIN_RATE: float = 0.70
STRONG_MAX_WIN_RATE: float = 0.90  # above this → ELITE candidate

# Demotion thresholds (slightly looser than promotion to prevent flapping
# wallet → ELITE → STRONG → ELITE every weekly run as samples drift).
DEMOTE_ELITE_MIN_RESOLVED: int = 90   # 10% buffer below promotion
DEMOTE_ELITE_MIN_WIN_RATE: float = 0.85  # 5pp buffer below promotion
DEMOTE_STRONG_MIN_RESOLVED: int = 60  # below this STRONG → drop
DEMOTE_STRONG_MIN_WIN_RATE: float = 0.65

# Cohort safety
COHORT_MIN_SIZE: int = 30  # never let cohort drop below this
COHORT_DELTA_NOTIFY_PCT: float = 0.10  # notify if cohort changes > 10%

# Legacy alias kept for code that imports the old constant — delete in v0.8.
ELITE_MIN_COMPOSITE_SCORE: float = 0.0  # unused gate (kept for import compat)
# Old names mapped to the new demote thresholds (test imports rely on them)
DEMOTE_THRESHOLD_RESOLVED: int = DEMOTE_ELITE_MIN_RESOLVED
DEMOTE_THRESHOLD_WIN_RATE: float = DEMOTE_ELITE_MIN_WIN_RATE

# Backup retention
BACKUP_RETENTION_DAYS: int = 30

# 2026-05-16 — Edge decay auto-demote (spec.md règle 7 cascade étape 2).
# Adds a ROLLING WR check on paper trade history. If a wallet's lifetime
# stats still pass ELITE bar but its recent paper performance is decayed,
# auto-demote at next daily cron pass. Safeguard : fallback to lifetime-only
# check when paper sample < ROLLING_MIN_SAMPLE (= fresh wallets not tradés
# assez par bot pour avoir un signal rolling fiable).
ROLLING_RECENT_N: int = 30
ROLLING_MIN_WR_FOR_ELITE: float = 0.70  # below this on recent N = edge decay
ROLLING_MIN_SAMPLE: int = 30  # need ≥N closed paper trades to apply check


def _compute_rolling_paper_wr(session, address: str, n: int = ROLLING_RECENT_N) -> tuple[int, float]:
    """Return (n_closed_sample, win_rate) for last N closed paper trades.

    Used by _evaluate_for_demotion to detect edge decay on ELITE wallets
    whose lifetime stats still look fine but recent paper performance is
    degraded. Returns (0, 0.0) when paper sample is empty.
    """
    try:
        from app.models.trade import PaperTrade
        from sqlmodel import select as _select
        rows = list(session.exec(
            _select(PaperTrade)
            .where(PaperTrade.wallet_address == address)
            .where(PaperTrade.status == "closed")
            .order_by(PaperTrade.closed_at.desc())
            .limit(n)
        ))
        if not rows:
            return (0, 0.0)
        wins = sum(1 for r in rows if (r.realized_pnl or 0) > 0)
        return (len(rows), wins / len(rows))
    except Exception:
        return (0, 0.0)


@dataclass
class ReclassDecision:
    address: str
    previous_status: Optional[str]
    new_status: str
    reason: str
    composite_score: Optional[float] = None
    resolved_markets: Optional[int] = None
    win_rate: Optional[float] = None


@dataclass
class ReclassResult:
    started_at: str
    finished_at: str
    cohort_before: int
    cohort_after: int
    promoted: list[ReclassDecision] = field(default_factory=list)
    demoted: list[ReclassDecision] = field(default_factory=list)
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    db_backup_path: Optional[str] = None
    rolled_back: bool = False

    def cohort_delta_pct(self) -> float:
        if self.cohort_before == 0:
            return 0.0
        return abs(self.cohort_after - self.cohort_before) / self.cohort_before


def _backup_db(db_path: Path, backup_dir: Path) -> Path:
    """Idempotent DB backup. Returns the backup file path."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"polyoracle_pre_reclass_{timestamp}.db"
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    shutil.copy2(db_path, backup_path)
    logger.info("reclass: DB backed up to %s", backup_path)
    return backup_path


def _prune_old_backups(backup_dir: Path, retention_days: int = BACKUP_RETENTION_DAYS) -> int:
    """Delete backups older than retention_days. Returns count of deleted."""
    if not backup_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = 0
    for f in backup_dir.glob("polyoracle_pre_reclass_*.db"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError as e:
            logger.warning("reclass: failed to prune %s: %s", f, e)
    return deleted


def _classify(resolved: int, win_rate: float) -> str:
    """Pure classifier — returns the canonical status for a wallet given
    its resolved_markets_traded and resolved_market_win_rate. v0.7.8 P6
    operator contract. Win rate gates first since it's the discriminating
    edge signal; sample size is the confidence floor."""
    if resolved >= ELITE_MIN_RESOLVED_MARKETS and win_rate >= ELITE_MIN_WIN_RATE:
        return "ELITE"
    if (
        resolved >= STRONG_MIN_RESOLVED_MARKETS
        and STRONG_MIN_WIN_RATE <= win_rate < STRONG_MAX_WIN_RATE
    ):
        return "STRONG"
    # ELITE-class win rate but undersized sample → STRONG (still tradable
    # if it has enough trades for the STRONG floor).
    if (
        resolved >= STRONG_MIN_RESOLVED_MARKETS
        and win_rate >= ELITE_MIN_WIN_RATE
    ):
        return "STRONG"
    return "DROPPED"


def _evaluate_for_promotion(row, current_status: str) -> Optional[ReclassDecision]:
    """Apply ELITE promotion criteria. Returns ReclassDecision if action
    needed, None if status unchanged.

    2026-05-09 — B1 fix: use REAL sample (resolved_winning + resolved_losing),
    not resolved_markets_traded which inflates by including unresolved scalped
    markets. spec.md spec: 'sample = wins + losses CONFIRMÉS'.
    """
    resolved = int((row.resolved_winning_markets or 0) + (row.resolved_losing_markets or 0))
    win_rate = float(row.resolved_market_win_rate or 0)

    target = _classify(resolved, win_rate)
    if target == "ELITE" and current_status != "ELITE":
        return ReclassDecision(
            address=row.address,
            previous_status=current_status,
            new_status="ELITE",
            reason=f"meets ELITE criteria (resolved={resolved}, win={win_rate:.3f})",
            composite_score=float(row.composite_score or 0),
            resolved_markets=resolved,
            win_rate=win_rate,
        )
    if target == "STRONG" and current_status not in ("STRONG", "ELITE"):
        return ReclassDecision(
            address=row.address,
            previous_status=current_status,
            new_status="STRONG",
            reason=f"meets STRONG criteria (resolved={resolved}, win={win_rate:.3f})",
            composite_score=float(row.composite_score or 0),
            resolved_markets=resolved,
            win_rate=win_rate,
        )
    return None


def _evaluate_for_demotion(row, current_status: str, session=None) -> Optional[ReclassDecision]:
    """Apply demotion criteria with hysteresis (looser than promotion to
    prevent flapping ELITE↔STRONG).

    2026-05-09 — B1 fix: use REAL sample (W+L confirmés), not resolved_markets_traded.
    2026-05-16 — Edge decay check : if session provided, also check rolling paper
    WR. Lifetime-OK ELITE with rolling_30 WR < ROLLING_MIN_WR_FOR_ELITE on sample
    ≥ ROLLING_MIN_SAMPLE → auto-demote ELITE→STRONG (spec.md règle 7 cascade
    étape 2 'edge decay'). Safeguard : sample < threshold = fallback lifetime only.
    """
    resolved = int((row.resolved_winning_markets or 0) + (row.resolved_losing_markets or 0))
    win_rate = float(row.resolved_market_win_rate or 0)

    if current_status == "ELITE":
        lifetime_fail = (
            resolved < DEMOTE_ELITE_MIN_RESOLVED
            or win_rate < DEMOTE_ELITE_MIN_WIN_RATE
        )
        # 2026-05-16 rolling edge decay check (only if session available)
        rolling_fail = False
        rolling_reason = ""
        if session is not None and not lifetime_fail:
            rolling_n, rolling_wr = _compute_rolling_paper_wr(session, row.address)
            if rolling_n >= ROLLING_MIN_SAMPLE and rolling_wr < ROLLING_MIN_WR_FOR_ELITE:
                rolling_fail = True
                rolling_reason = f"rolling_{rolling_n}_wr={rolling_wr:.3f} < {ROLLING_MIN_WR_FOR_ELITE}"

        if lifetime_fail or rolling_fail:
            new_status = (
                "STRONG"
                if resolved >= STRONG_MIN_RESOLVED_MARKETS
                and win_rate >= STRONG_MIN_WIN_RATE
                else "DROPPED"
            )
            if lifetime_fail:
                reason = f"failed ELITE bar (resolved={resolved}, win={win_rate:.3f})"
            else:
                reason = f"edge_decay {rolling_reason} (lifetime {resolved}/{win_rate:.3f} OK)"
            return ReclassDecision(
                address=row.address,
                previous_status="ELITE",
                new_status=new_status,
                reason=reason,
                composite_score=float(row.composite_score or 0),
                resolved_markets=resolved,
                win_rate=win_rate,
            )
    elif current_status == "STRONG":
        if (
            resolved < DEMOTE_STRONG_MIN_RESOLVED
            or win_rate < DEMOTE_STRONG_MIN_WIN_RATE
        ):
            return ReclassDecision(
                address=row.address,
                previous_status="STRONG",
                new_status="DROPPED",
                reason=f"failed STRONG bar (resolved={resolved}, win={win_rate:.3f})",
                composite_score=float(row.composite_score or 0),
                resolved_markets=resolved,
                win_rate=win_rate,
            )
    return None


def run_weekly_reclass(
    session: Session,
    *,
    db_path: Path,
    backup_dir: Path,
    dry_run: bool = False,
) -> ReclassResult:
    """Single weekly reclass iteration. NEVER throws — all failures
    captured in result.errors and result.rolled_back.

    Args:
        session: SQLModel session
        db_path: Path to the SQLite file (for backup)
        backup_dir: Where to store the pre-reclass backup
        dry_run: if True, compute decisions but don't write to DB

    Returns: ReclassResult with full audit trail.
    """
    from app.models.wallet import MarketFirstWalletRecord

    started = datetime.now(timezone.utc)
    result = ReclassResult(
        started_at=started.isoformat(timespec="seconds"),
        finished_at="",
        cohort_before=0,
        cohort_after=0,
    )

    # Step 1 — backup
    if not dry_run:
        try:
            backup_path = _backup_db(db_path, backup_dir)
            result.db_backup_path = str(backup_path)
        except Exception as e:
            result.errors.append(f"backup_failed: {e}")
            result.rolled_back = True
            result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return result

    # Step 2 — read current ELITE cohort size (= "cohort_before")
    try:
        result.cohort_before = session.exec(
            select(MarketFirstWalletRecord).where(
                MarketFirstWalletRecord.candidate_status == "ELITE"
            )
        ).all()
        result.cohort_before = len(result.cohort_before)
    except Exception as e:
        result.errors.append(f"cohort_count_failed: {e}")
        result.rolled_back = True
        result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return result

    # Step 3 — evaluate all MFWR rows for promotion/demotion
    try:
        rows = list(session.exec(select(MarketFirstWalletRecord)))
    except Exception as e:
        result.errors.append(f"mfwr_query_failed: {e}")
        result.rolled_back = True
        result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return result

    for row in rows:
        try:
            promote = _evaluate_for_promotion(row, row.candidate_status or "")
            if promote is not None:
                result.promoted.append(promote)
                if not dry_run:
                    # 2026-05-09 fix: was hardcoding "ELITE" — promotions to STRONG
                    # were silently marked ELITE, polluting the cohort. Use the
                    # evaluator's target (STRONG or ELITE) which respects W+L gate.
                    row.candidate_status = promote.new_status
                    session.add(row)
                continue
            demote = _evaluate_for_demotion(row, row.candidate_status or "", session=session)
            if demote is not None:
                result.demoted.append(demote)
                if not dry_run:
                    # v0.7.8 P6 — use the evaluator's target (STRONG or DROPPED).
                    # ELITE that fails ELITE bar but still meets STRONG → STRONG.
                    # ELITE/STRONG that fails STRONG bar → DROPPED. Previously
                    # this hard-coded STRONG, which prevented ELITE→DROPPED.
                    row.candidate_status = demote.new_status
                    session.add(row)
                continue
            result.unchanged += 1
        except Exception as e:
            result.errors.append(f"row_eval_failed addr={row.address}: {e}")

    # Step 4 — compute new cohort, validate min size
    new_cohort_size = result.cohort_before + len(result.promoted) - len(result.demoted)

    # Cohort safety: NEVER let it drop below COHORT_MIN_SIZE
    if new_cohort_size < COHORT_MIN_SIZE:
        # Rollback: don't commit any changes
        result.errors.append(
            f"COHORT_SAFETY: new size {new_cohort_size} < min {COHORT_MIN_SIZE}, "
            f"rolling back all changes"
        )
        result.rolled_back = True
        if not dry_run:
            session.rollback()
        result.cohort_after = result.cohort_before
        result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return result

    # Step 5 — commit (or skip in dry_run)
    if not dry_run:
        try:
            session.commit()
        except Exception as e:
            result.errors.append(f"commit_failed: {e}")
            result.rolled_back = True
            session.rollback()
            result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return result

    result.cohort_after = new_cohort_size

    # Step 6 — prune old backups
    if not dry_run:
        try:
            pruned = _prune_old_backups(backup_dir)
            if pruned > 0:
                logger.info("reclass: pruned %d old backups", pruned)
        except Exception as e:
            result.errors.append(f"prune_failed: {e}")  # non-fatal

    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Notify if change > 10%
    if result.cohort_delta_pct() > COHORT_DELTA_NOTIFY_PCT:
        logger.warning(
            "reclass: cohort changed %.1f%% (before=%d, after=%d) — significant",
            result.cohort_delta_pct() * 100,
            result.cohort_before, result.cohort_after,
        )
    else:
        logger.info(
            "reclass: cohort delta %.1f%% (before=%d, after=%d, promoted=%d, demoted=%d)",
            result.cohort_delta_pct() * 100,
            result.cohort_before, result.cohort_after,
            len(result.promoted), len(result.demoted),
        )

    return result


def reclass_summary_md(result: ReclassResult) -> str:
    """Generate a markdown summary for the reclass log."""
    lines = [f"# Reclass run — {result.started_at}\n"]
    lines.append(f"**Cohort before**: {result.cohort_before} ELITE")
    lines.append(f"**Cohort after**: {result.cohort_after} ELITE")
    lines.append(f"**Promoted**: {len(result.promoted)}")
    lines.append(f"**Demoted**: {len(result.demoted)}")
    lines.append(f"**Unchanged**: {result.unchanged}")
    lines.append(f"**Errors**: {len(result.errors)}")
    lines.append(f"**Rolled back**: {result.rolled_back}")
    if result.db_backup_path:
        lines.append(f"**DB backup**: `{result.db_backup_path}`")
    if result.errors:
        lines.append("\n## Errors")
        for e in result.errors[:20]:
            lines.append(f"- {e}")
    if result.promoted:
        lines.append("\n## Promotions (first 20)")
        for d in result.promoted[:20]:
            lines.append(
                f"- `{d.address[:14]}` {d.previous_status} → {d.new_status}: "
                f"comp={d.composite_score:.1f}, W+L={d.resolved_markets}, "
                f"win={d.win_rate:.3f}"
            )
    if result.demoted:
        lines.append("\n## Demotions (first 20)")
        for d in result.demoted[:20]:
            lines.append(
                f"- `{d.address[:14]}` {d.previous_status} → {d.new_status}: "
                f"comp={d.composite_score:.1f}, W+L={d.resolved_markets}, "
                f"win={d.win_rate:.3f}"
            )
    return "\n".join(lines)
