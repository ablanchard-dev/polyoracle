"""P4-C copyable_wallet_score engine (P1, review Round 8 — 2026-05-12).

Computes per-wallet a single score [0, 1] that measures HOW COPIABLE a wallet
is by THIS bot, not just how good the wallet is in absolute terms. Two
wallets can have wr=99% but be very different to copy:
  - one trades 5min crypto where we poll fast → high copyability
  - one trades 90-day politics where we never see the move → low copyability

Formula (review Round 8 verbatim):
    score =
      0.20 * resolved_wr_quality        # Wilson 95% LB on (W+L)
    + 0.15 * sample_size_confidence     # sqrt(min(W+L, 1000) / 1000)
    + 0.15 * recent_7d_14d_edge         # WR trending up vs lifetime
    + 0.15 * live_poll_rate             # % source trades seen LIVE_POLL
    + 0.10 * low_copy_delay_score       # 1 - (copy_delay_p50 / 120s)
    + 0.10 * category_known_rate        # % of bot trades NOT in UNKNOWN_CATEGORY
    + 0.10 * gamma_clob_consistency     # % bot entries near gamma_mid
    + 0.05 * wallet_activity_score      # MFWR.recent_activity_score / 100
    - penalty_unknown_category          # rate of UNKNOWN_CATEGORY rejects
    - penalty_delayed_poll              # rate of DELAYED_POLL source trades
    - penalty_source_exit_ambiguity     # rate of SELL_EXIT vs entries
    - penalty_bad_recent_streak         # >=5 consecutive losses (binary)

Sub-scores that need post-P0 data (EntryPriceAudit, M1 v5 metrics) will
return None when data is insufficient. The aggregate score falls back to
the available components with proportionally-renormalized weights.

Usage:
    from app.services.copyable_wallet_score import compute_wallet_score
    breakdown = compute_wallet_score(session, wallet_address)
    if breakdown.score >= 0.75:
        # high-quality copyable wallet

Live gates (Round 8 thresholds, to be calibrated after first 6h+ data):
    - score >= 0.75 → priority lane
    - score >= 0.60 → standard
    - score < 0.40 → demotion candidate (after sample_size_min reached)
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select


# ---------- Formula weights (Round 8 verbatim) ----------
WEIGHTS: dict[str, float] = {
    "resolved_wr_quality": 0.20,
    "sample_size_confidence": 0.15,
    "recent_7d_14d_edge": 0.15,
    "live_poll_rate": 0.15,
    "low_copy_delay_score": 0.10,
    "category_known_rate": 0.10,
    "gamma_clob_consistency": 0.10,
    "wallet_activity_score": 0.05,
}

# Penalty multipliers (subtractive, capped)
PENALTY_WEIGHTS: dict[str, float] = {
    "unknown_category_rate": 0.10,
    "delayed_poll_rate": 0.10,
    "source_exit_ambiguity_rate": 0.05,
    "bad_recent_streak_binary": 0.10,
}

# Threshold for "live_poll": source.seen_at - source.traded_at ≤ N seconds
LIVE_POLL_FETCH_DELAY_S = 60.0

# Threshold for "low copy delay": bot.opened_at - source.traded_at ≤ N seconds
COPY_DELAY_TARGET_S = 120.0

# Threshold for "gamma_clob_consistency": |chosen - gamma_mid| / gamma_mid ≤ X
GAMMA_CONSISTENCY_TOLERANCE = 0.05

# Bad streak threshold (5 consecutive losses = anomaly)
BAD_STREAK_THRESHOLD = 5

# Sample size confidence saturation
SAMPLE_SATURATION = 1000


@dataclass
class CopyableScoreBreakdown:
    wallet_address: str
    score: float | None
    # Component sub-scores (None if insufficient data)
    resolved_wr_quality: float | None = None
    sample_size_confidence: float | None = None
    recent_7d_14d_edge: float | None = None
    live_poll_rate: float | None = None
    low_copy_delay_score: float | None = None
    category_known_rate: float | None = None
    gamma_clob_consistency: float | None = None
    wallet_activity_score: float | None = None
    # Penalties
    penalty_unknown_category: float = 0.0
    penalty_delayed_poll: float = 0.0
    penalty_source_exit_ambiguity: float = 0.0
    penalty_bad_recent_streak: float = 0.0
    # Diagnostics
    weights_applied: dict[str, float] = field(default_factory=dict)
    weights_total_applied: float = 0.0
    notes: list[str] = field(default_factory=list)


# ---------- Helpers ----------


def _wilson_lower_bound(p: float, n: int, z: float = 1.96) -> float:
    """Wilson score 95% lower bound. Returns 0 when n=0."""
    if n <= 0:
        return 0.0
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / denom)


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------- Sub-score computations ----------


def _compute_resolved_wr_quality(session: Session, wallet_address: str) -> tuple[float | None, dict]:
    """Wilson 95% LB of resolved_market_win_rate, gates on sample >= 30."""
    from app.models.wallet import MarketFirstWalletRecord
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(
            MarketFirstWalletRecord.address == wallet_address.lower()
        )
    ).first()
    if mfwr is None:
        return (None, {"reason": "no_mfwr"})
    wins = mfwr.resolved_winning_markets or 0
    losses = mfwr.resolved_losing_markets or 0
    n = wins + losses
    if n < 30:
        return (None, {"reason": "insufficient_sample", "n": n})
    wr = mfwr.resolved_market_win_rate or 0.0
    return (_wilson_lower_bound(wr, n), {"wr": wr, "n": n})


def _compute_sample_size_confidence(session: Session, wallet_address: str) -> tuple[float | None, dict]:
    """sqrt(min(W+L, 1000) / 1000) — caps at full confidence at 1000 trades."""
    from app.models.wallet import MarketFirstWalletRecord
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(
            MarketFirstWalletRecord.address == wallet_address.lower()
        )
    ).first()
    if mfwr is None:
        return (None, {"reason": "no_mfwr"})
    n = (mfwr.resolved_winning_markets or 0) + (mfwr.resolved_losing_markets or 0)
    return (math.sqrt(min(n, SAMPLE_SATURATION) / SAMPLE_SATURATION), {"n": n})


def _compute_live_poll_rate(session: Session, wallet_address: str, window_days: int = 30) -> tuple[float | None, dict]:
    """% of source trades whose seen_at-traded_at ≤ LIVE_POLL_FETCH_DELAY_S.

    Reads from PublicTrade. Need >=20 trades to be meaningful.
    """
    from app.models.trade import PublicTrade
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    rows = list(session.exec(
        select(PublicTrade)
        .where(PublicTrade.wallet_address == wallet_address.lower())
        .where(PublicTrade.seen_at >= cutoff)
    ))
    if len(rows) < 20:
        return (None, {"reason": "insufficient_trades", "n": len(rows)})
    live = 0
    delayed = 0
    for r in rows:
        if r.seen_at is None or r.traded_at is None:
            continue
        seen = _ensure_aware(r.seen_at)
        traded = _ensure_aware(r.traded_at)
        delay = (seen - traded).total_seconds()
        if delay <= LIVE_POLL_FETCH_DELAY_S:
            live += 1
        else:
            delayed += 1
    total = live + delayed
    if total == 0:
        return (None, {"reason": "no_timestamped_trades"})
    return (live / total, {"live": live, "delayed": delayed, "total": total})


def _compute_low_copy_delay_score(session: Session, wallet_address: str) -> tuple[float | None, dict]:
    """1 - (copy_delay_p50 / 120s) clamped to [0, 1].

    Reads from EntryPriceAudit (post-P0.4). Pre-P0 data unavailable → None.
    """
    try:
        from app.models.trade import EntryPriceAudit, PaperTrade
    except Exception:
        return (None, {"reason": "table_not_ready"})
    # Need bot.opened_at and source.traded_at — bot.opened_at comes from PaperTrade.
    # source.traded_at lives in EntryPriceAudit indirectly (via source_trade_id).
    # For simplicity: read entries with source_trade_id, join PublicTrade.traded_at.
    from app.models.trade import PublicTrade
    rows = list(session.exec(
        select(EntryPriceAudit, PublicTrade)
        .join(PublicTrade, PublicTrade.id == EntryPriceAudit.source_trade_id)
        .where(EntryPriceAudit.source_wallet == wallet_address.lower())
    ))
    delays: list[float] = []
    for entry, pt in rows:
        if pt.traded_at and entry.opened_at:
            traded = _ensure_aware(pt.traded_at)
            opened = _ensure_aware(entry.opened_at)
            delays.append((opened - traded).total_seconds())
    if len(delays) < 5:
        return (None, {"reason": "insufficient_audits", "n": len(delays)})
    delays.sort()
    p50 = delays[len(delays) // 2]
    score = max(0.0, min(1.0, 1.0 - p50 / COPY_DELAY_TARGET_S))
    return (score, {"p50_s": p50, "n": len(delays)})


def _compute_category_known_rate(session: Session, wallet_address: str) -> tuple[float | None, dict]:
    """% of bot trades closed with resolved_category != Unknown (post-P0.5).

    Reads close_reason metadata.
    """
    import json as _json
    from app.models.trade import PaperTrade
    rows = list(session.exec(
        select(PaperTrade)
        .where(PaperTrade.wallet_address == wallet_address.lower())
        .where(PaperTrade.status == "closed")
    ))
    if len(rows) < 10:
        return (None, {"reason": "insufficient_closed", "n": len(rows)})
    known = 0
    total = 0
    for tr in rows:
        cr = tr.close_reason
        if not cr or "|" not in cr:
            total += 1
            continue
        try:
            meta = _json.loads(cr.split("|", 1)[1])
            cat = meta.get("resolved_category") or meta.get("category") or "Unknown"
            total += 1
            if cat.lower() not in ("unknown", "uncategorised", "uncategorized", ""):
                known += 1
        except Exception:
            total += 1
    if total == 0:
        return (None, {"reason": "no_parseable_close_reason"})
    return (known / total, {"known": known, "total": total})


def _compute_gamma_clob_consistency(session: Session, wallet_address: str) -> tuple[float | None, dict]:
    """% of EntryPriceAudit rows where chosen_entry is consistent with gamma_mid.

    Consistency = |chosen - gamma| / gamma ≤ tolerance (5%). Indicates that
    the bot's price source agreed with the live market — predictor of low
    paper-vs-live haircut.
    """
    try:
        from app.models.trade import EntryPriceAudit
    except Exception:
        return (None, {"reason": "table_not_ready"})
    rows = list(session.exec(
        select(EntryPriceAudit)
        .where(EntryPriceAudit.source_wallet == wallet_address.lower())
        .where(EntryPriceAudit.gamma_mid_at_open.is_not(None))  # type: ignore[union-attr]
    ))
    if len(rows) < 5:
        return (None, {"reason": "insufficient_audits", "n": len(rows)})
    consistent = 0
    for r in rows:
        if r.gamma_mid_at_open is None or r.gamma_mid_at_open == 0:
            continue
        diff = abs(r.chosen_entry_price - r.gamma_mid_at_open) / r.gamma_mid_at_open
        if diff <= GAMMA_CONSISTENCY_TOLERANCE:
            consistent += 1
    return (consistent / len(rows), {"consistent": consistent, "n": len(rows)})


def _compute_wallet_activity_score(session: Session, wallet_address: str) -> tuple[float | None, dict]:
    """MFWR.recent_activity_score normalized to [0, 1]."""
    from app.models.wallet import MarketFirstWalletRecord
    mfwr = session.exec(
        select(MarketFirstWalletRecord).where(
            MarketFirstWalletRecord.address == wallet_address.lower()
        )
    ).first()
    if mfwr is None or mfwr.recent_activity_score is None:
        return (None, {"reason": "no_mfwr"})
    return (min(1.0, max(0.0, mfwr.recent_activity_score / 100.0)),
            {"raw": mfwr.recent_activity_score})


def _compute_bad_recent_streak(session: Session, wallet_address: str) -> tuple[float, dict]:
    """Binary penalty: 1 if last 5 closed trades all lost, else 0."""
    from app.models.trade import PaperTrade
    rows = list(session.exec(
        select(PaperTrade)
        .where(PaperTrade.wallet_address == wallet_address.lower())
        .where(PaperTrade.status == "closed")
        .order_by(PaperTrade.closed_at.desc())  # type: ignore[union-attr]
        .limit(BAD_STREAK_THRESHOLD)
    ))
    if len(rows) < BAD_STREAK_THRESHOLD:
        return (0.0, {"reason": "insufficient_trades"})
    all_losses = all((r.realized_pnl or 0.0) <= 0 for r in rows)
    return (1.0 if all_losses else 0.0, {"all_losses": all_losses, "n": len(rows)})


# ---------- Aggregate ----------


def compute_wallet_score(
    session: Session, wallet_address: str, *,
    skip_penalties: bool = False,
) -> CopyableScoreBreakdown:
    """Compute the copyable wallet score for a single wallet.

    Returns CopyableScoreBreakdown with `score` in [0, 1] or None if no
    sub-score is computable. Weights are proportionally re-normalized over
    the available sub-scores so that partial-data wallets still get a score
    (with a note in `notes`).
    """
    addr = wallet_address.lower()
    breakdown = CopyableScoreBreakdown(wallet_address=addr, score=None)

    # Sub-scores
    breakdown.resolved_wr_quality, _ = _compute_resolved_wr_quality(session, addr)
    breakdown.sample_size_confidence, _ = _compute_sample_size_confidence(session, addr)
    breakdown.live_poll_rate, _ = _compute_live_poll_rate(session, addr)
    breakdown.low_copy_delay_score, _ = _compute_low_copy_delay_score(session, addr)
    breakdown.category_known_rate, _ = _compute_category_known_rate(session, addr)
    breakdown.gamma_clob_consistency, _ = _compute_gamma_clob_consistency(session, addr)
    breakdown.wallet_activity_score, _ = _compute_wallet_activity_score(session, addr)
    breakdown.recent_7d_14d_edge = None  # TBD — needs trade-history-by-date

    # Penalties
    if not skip_penalties:
        bad_streak, _ = _compute_bad_recent_streak(session, addr)
        breakdown.penalty_bad_recent_streak = bad_streak * PENALTY_WEIGHTS["bad_recent_streak_binary"]

    # Aggregate with re-normalization on available components
    components = {
        "resolved_wr_quality": breakdown.resolved_wr_quality,
        "sample_size_confidence": breakdown.sample_size_confidence,
        "recent_7d_14d_edge": breakdown.recent_7d_14d_edge,
        "live_poll_rate": breakdown.live_poll_rate,
        "low_copy_delay_score": breakdown.low_copy_delay_score,
        "category_known_rate": breakdown.category_known_rate,
        "gamma_clob_consistency": breakdown.gamma_clob_consistency,
        "wallet_activity_score": breakdown.wallet_activity_score,
    }
    available = {k: v for k, v in components.items() if v is not None}
    if not available:
        breakdown.notes.append("no sub-score available")
        return breakdown

    weight_total_avail = sum(WEIGHTS[k] for k in available)
    weight_total_all = sum(WEIGHTS.values())
    if weight_total_avail < weight_total_all - 1e-9:
        breakdown.notes.append(
            f"partial scoring: {len(available)}/{len(components)} sub-scores available "
            f"({weight_total_avail:.2f}/{weight_total_all:.2f} weight)"
        )

    weighted_sum = sum(WEIGHTS[k] * available[k] for k in available)
    # Re-normalize to [0, 1] over available weights
    score = weighted_sum / weight_total_avail if weight_total_avail > 0 else 0.0

    # Apply penalties (subtractive, never push below 0)
    penalty_total = (
        breakdown.penalty_unknown_category
        + breakdown.penalty_delayed_poll
        + breakdown.penalty_source_exit_ambiguity
        + breakdown.penalty_bad_recent_streak
    )
    score = max(0.0, score - penalty_total)
    breakdown.score = round(score, 4)
    breakdown.weights_applied = {k: WEIGHTS[k] for k in available}
    breakdown.weights_total_applied = round(weight_total_avail, 4)
    return breakdown


def compute_scores_for_pool(
    session: Session, *, min_wl_total: int = 80,
) -> list[CopyableScoreBreakdown]:
    """Run compute_wallet_score over all wallets with W+L >= min_wl_total."""
    from app.models.wallet import MarketFirstWalletRecord
    addresses = [
        r.address for r in session.exec(
            select(MarketFirstWalletRecord).where(
                (MarketFirstWalletRecord.resolved_winning_markets
                 + MarketFirstWalletRecord.resolved_losing_markets) >= min_wl_total
            )
        )
    ]
    return [compute_wallet_score(session, a) for a in addresses]
