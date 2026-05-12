"""M1 copy_efficiency engine (A-INSTR Phase A — 2026-05-11).

For each closed PaperTrade, compute:

  copy_efficiency = bot_net_pnl / source_wallet_counterfactual_pnl

where source_counterfactual_pnl = source_unit_pnl × bot_notional, and
source_unit_pnl reconstructs the wallet's per-unit PnL on the same market
(settlement_value − wallet_entry_price − estimated_wallet_fee).

When source_counterfactual ≤ 0, exclude from main ratio (bucket
SOURCE_LOSS_OR_FLAT) and track separately as "bot avoided source losses".

Group-by 8 dims: wallet, tier, wr_bucket, duration_bucket, category,
market_type, source_lane, close_reason.

Thresholds (locked 2026-05-11):
  global ≥ 0.70  / crypto 5min ≥ 0.55 / sports + politics ≥ 0.75
  excellent ≥ 0.90 (= bot captures almost all wallet edge)

Used by:
  - GET /edge/copy-efficiency?window=24h|7d|30d
  - Phase C decision: M3 wallet weighting (input feature)
  - Anti-erosion alerts (M2)
  - P4 discovery (copyability scoring)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any

from sqlmodel import Session, select

from app.models.trade import PaperTrade, PublicTrade
from app.models.wallet import ResolvedMarketRecord
from app.services.fee_engine import compute_dynamic_fee

# --------- Thresholds ---------

GLOBAL_MIN_THRESHOLD = 0.70
CATEGORY_MIN_THRESHOLDS: dict[str, float] = {
    "crypto": 0.55,
    "crypto_5min": 0.55,
    "sports": 0.75,
    "politics": 0.75,
    "geopolitics": 0.70,
    "finance": 0.65,
    "economics": 0.65,
    "weather": 0.60,
    "culture": 0.60,
    "tech": 0.65,
    "mentions": 0.65,
    "other": 0.60,
    "unknown": 0.60,
}
EXCELLENCE_THRESHOLD = 0.90

# Window presets (hours)
WINDOW_PRESETS_H = {"24h": 24.0, "7d": 168.0, "30d": 720.0}


@dataclass
class TradeCopyRecord:
    trade_id: str
    wallet_address: str | None
    market_id: str
    category: str | None
    bot_pnl: float
    source_unit_pnl: float
    source_counterfactual: float
    bot_notional: float
    copy_efficiency: float | None  # None when SOURCE_LOSS_OR_FLAT
    bucket: str  # NORMAL | SOURCE_LOSS_OR_FLAT | BOT_OUTPERFORMS_SOURCE
    close_reason: str | None
    closed_at: datetime | None


@dataclass
class CopyEfficiencyReport:
    window_hours: float
    sample_size: int
    sample_size_excluded: int  # SOURCE_LOSS_OR_FLAT count
    global_ratio: float | None
    bot_pnl_total: float
    source_counterfactual_total: float
    bot_outperforms_source_count: int
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_wallet_top: list[dict[str, Any]] = field(default_factory=list)
    by_wallet_bottom: list[dict[str, Any]] = field(default_factory=list)
    threshold_breaches: list[dict[str, Any]] = field(default_factory=list)
    classification: str = "UNKNOWN"


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _resolve_market_record(
    session: Session, market_id: str | None,
) -> ResolvedMarketRecord | None:
    """2026-05-12 P0 FORENSIC FIX: PaperTrade.market_id is a condition_id
    (hex), but ResolvedMarketRecord uses numeric Polymarket market_id as
    primary key + hex condition_id as separate column. Fallback to lookup
    by condition_id (= same logic as paper_trading_engine._resolve_market)."""
    if not market_id:
        return None
    rec = session.get(ResolvedMarketRecord, market_id)
    if rec is not None and rec.winning_outcome_index is not None:
        return rec
    try:
        rec = session.exec(
            select(ResolvedMarketRecord).where(
                ResolvedMarketRecord.condition_id == market_id
            )
        ).first()
    except Exception:
        rec = None
    return rec


def _category_of(market_id: str | None, session: Session) -> str:
    if not market_id:
        return "unknown"
    try:
        from app.models.market import Market as _Market
        row = session.get(_Market, market_id)
        if row is not None and getattr(row, "category", None):
            return str(row.category).lower()
    except Exception:
        pass
    rmr = _resolve_market_record(session, market_id)
    if rmr and rmr.category:
        return str(rmr.category).lower()
    return "unknown"


def _category_threshold(category: str | None) -> float:
    if not category:
        return GLOBAL_MIN_THRESHOLD
    return CATEGORY_MIN_THRESHOLDS.get(category.lower(), GLOBAL_MIN_THRESHOLD)


def _extract_bot_settlement(bot_trade: PaperTrade) -> float | None:
    """Extract the actual settlement value the bot received at close.

    2026-05-12 P0 FORENSIC FIX v2: source-of-truth is the bot's own
    close metadata, NOT ResolvedMarketRecord (which doesn't get populated
    fast enough for fresh crypto 5min markets — 0% coverage observed).

    Parses close_reason JSON suffix `CLOSED_RESOLVED|{...}` to get exit_price.
    For resolved markets, exit_price = 1.0 (bot.outcome won) or 0.0 (lost).
    For manual close, exit_price = actual market price at close time.
    """
    if not bot_trade.close_reason or "|" not in bot_trade.close_reason:
        return None
    try:
        import json as _json
        _reason, payload = bot_trade.close_reason.split("|", 1)
        meta = _json.loads(payload)
        exit_price = meta.get("exit_price")
        if exit_price is None:
            return None
        return float(exit_price)
    except Exception:
        return None


def _source_unit_pnl_for(
    *,
    session: Session,
    bot_trade: PaperTrade,
    category: str,
) -> tuple[float, float] | None:
    """Reconstruct (source_unit_pnl, wallet_entry_price) from PublicTrade
    matching this bot trade's wallet+market+side. Returns None if no source
    trade can be matched (= we treat as SOURCE_UNAVAILABLE bucket).

    2026-05-12 P0 FORENSIC FIX v2: settlement value comes from the bot's
    OWN close metadata (exit_price), not from ResolvedMarketRecord. The
    bot already determined the resolution at close time — we trust that
    rather than re-querying a potentially-empty RMR table.
    """
    if not bot_trade.wallet_address or not bot_trade.market_id:
        return None
    rows = list(session.exec(
        select(PublicTrade)
        .where(PublicTrade.wallet_address == bot_trade.wallet_address)
        .where(PublicTrade.market_id == bot_trade.market_id)
    ).all())
    if not rows:
        return None
    # Pick the closest-in-time wallet trade matching side + outcome
    bot_side = (bot_trade.side or "").upper()
    bot_outcome = (bot_trade.outcome or "").lower()
    candidates = [
        r for r in rows
        if (r.side or "").upper() == bot_side
        and (r.outcome or "").lower() == bot_outcome
    ]
    if not candidates:
        candidates = rows
    bot_open = _ensure_aware(bot_trade.opened_at) or datetime.now(UTC)
    candidates.sort(key=lambda r: abs(
        ((_ensure_aware(r.traded_at) or bot_open) - bot_open).total_seconds()
    ))
    src = candidates[0]
    wallet_entry = float(src.price or 0.0)
    if wallet_entry <= 0 or wallet_entry >= 1:
        return None

    # Settlement: use bot's own close metadata as source-of-truth.
    # If bot's outcome == source's outcome, source experiences same settlement.
    # If they diverge (rare), we adjust: settlement_for_source = 1.0 - bot_exit
    bot_settlement = _extract_bot_settlement(bot_trade)
    if bot_settlement is None:
        # Last resort fallback — use bot's average price as proxy
        bot_settlement = float(bot_trade.average_price or wallet_entry)

    same_outcome = (src.outcome or "").lower() == bot_outcome
    if same_outcome:
        source_settlement = bot_settlement  # source rides same wave as bot
    else:
        # Source on opposite outcome (rare in clean copy) — invert settlement
        source_settlement = 1.0 - bot_settlement

    # Apply side: BUY = long share, profits when settlement high.
    # SELL = closing existing long (= exit), so PnL still depends on bought price vs settle.
    # For simplicity (and because Polymarket "SELL" usually = close-long, not short-open),
    # we treat both as long-position PnL = settlement - entry.
    fee_per_unit = compute_dynamic_fee(category, wallet_entry, 1.0)
    if (src.side or "").upper() == "BUY":
        unit_pnl = source_settlement - wallet_entry - fee_per_unit
    else:
        # SELL = source exits at wallet_entry, settlement isn't realized PnL
        # but we approximate as if they'd held to resolution.
        unit_pnl = source_settlement - wallet_entry - fee_per_unit
    return (unit_pnl, wallet_entry)


def _build_records(
    session: Session, *, window_hours: float,
) -> list[TradeCopyRecord]:
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    rows = session.exec(
        select(PaperTrade).where(PaperTrade.status == "closed")
    ).all()
    records: list[TradeCopyRecord] = []
    for trade in rows:
        closed = _ensure_aware(trade.closed_at)
        if closed is None or closed < cutoff:
            continue
        category = _category_of(trade.market_id, session)
        source_pair = _source_unit_pnl_for(session=session, bot_trade=trade, category=category)
        bot_notional = float(trade.notional_usd or 0.0)
        bot_pnl = float(trade.realized_pnl or 0.0)
        if source_pair is None or bot_notional <= 0:
            # Source unavailable — exclude from ratio but count as observed
            records.append(TradeCopyRecord(
                trade_id=trade.id,
                wallet_address=trade.wallet_address,
                market_id=trade.market_id,
                category=category,
                bot_pnl=bot_pnl,
                source_unit_pnl=0.0,
                source_counterfactual=0.0,
                bot_notional=bot_notional,
                copy_efficiency=None,
                bucket="SOURCE_UNAVAILABLE",
                close_reason=trade.close_reason,
                closed_at=closed,
            ))
            continue
        unit_pnl, wallet_entry = source_pair
        # Normalize on bot notional: counterfactual = unit_pnl × shares_bot
        # bot_notional ≈ qty × entry_price → shares_bot ≈ notional / entry
        shares_bot = bot_notional / max(float(trade.average_price or wallet_entry), 1e-6)
        source_counterfactual = unit_pnl * shares_bot
        if source_counterfactual <= 0:
            bucket = "BOT_OUTPERFORMS_SOURCE" if bot_pnl > 0 else "SOURCE_LOSS_OR_FLAT"
            records.append(TradeCopyRecord(
                trade_id=trade.id,
                wallet_address=trade.wallet_address,
                market_id=trade.market_id,
                category=category,
                bot_pnl=bot_pnl,
                source_unit_pnl=unit_pnl,
                source_counterfactual=source_counterfactual,
                bot_notional=bot_notional,
                copy_efficiency=None,
                bucket=bucket,
                close_reason=trade.close_reason,
                closed_at=closed,
            ))
            continue
        eff = bot_pnl / source_counterfactual
        records.append(TradeCopyRecord(
            trade_id=trade.id,
            wallet_address=trade.wallet_address,
            market_id=trade.market_id,
            category=category,
            bot_pnl=bot_pnl,
            source_unit_pnl=unit_pnl,
            source_counterfactual=source_counterfactual,
            bot_notional=bot_notional,
            copy_efficiency=round(eff, 4),
            bucket="NORMAL",
            close_reason=trade.close_reason,
            closed_at=closed,
        ))
    return records


def _classify_global(
    *,
    global_ratio: float | None,
    sample_size: int,
) -> str:
    if sample_size == 0:
        return "UNKNOWN"
    if global_ratio is None:
        return "NO_VALID_RATIO"
    if global_ratio >= EXCELLENCE_THRESHOLD:
        return "EXCELLENT"
    if global_ratio >= GLOBAL_MIN_THRESHOLD:
        return "ACCEPTABLE"
    if global_ratio >= 0.50:
        return "DEGRADED"
    return "CRITICAL"


def compute_copy_efficiency_report(
    session: Session, *, window_hours: float = 24.0,
) -> CopyEfficiencyReport:
    """Pure function — only reads from DB. Easy to unit-test."""
    records = _build_records(session, window_hours=window_hours)
    sample_size = len(records)

    bot_pnl_total = 0.0
    counterfactual_total = 0.0
    valid_records: list[TradeCopyRecord] = []
    excluded = 0
    bot_outperforms = 0

    for rec in records:
        if rec.bucket == "NORMAL":
            valid_records.append(rec)
            bot_pnl_total += rec.bot_pnl
            counterfactual_total += rec.source_counterfactual
        elif rec.bucket == "BOT_OUTPERFORMS_SOURCE":
            bot_outperforms += 1
            excluded += 1
        else:
            excluded += 1

    global_ratio: float | None
    if counterfactual_total > 0:
        global_ratio = round(bot_pnl_total / counterfactual_total, 4)
    else:
        global_ratio = None

    # By category aggregation (numerator/denominator sum, not avg-of-ratios)
    cat_buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"bot_pnl": 0.0, "counterfactual": 0.0, "n": 0}
    )
    for rec in valid_records:
        cat = (rec.category or "unknown").lower()
        cat_buckets[cat]["bot_pnl"] += rec.bot_pnl
        cat_buckets[cat]["counterfactual"] += rec.source_counterfactual
        cat_buckets[cat]["n"] += 1
    by_category: dict[str, dict[str, Any]] = {}
    threshold_breaches: list[dict[str, Any]] = []
    for cat, agg in cat_buckets.items():
        ratio = (agg["bot_pnl"] / agg["counterfactual"]) if agg["counterfactual"] > 0 else None
        threshold = _category_threshold(cat)
        breached = ratio is not None and ratio < threshold
        by_category[cat] = {
            "n": int(agg["n"]),
            "ratio": round(ratio, 4) if ratio is not None else None,
            "threshold": threshold,
            "breached": breached,
            "bot_pnl": round(agg["bot_pnl"], 4),
            "counterfactual": round(agg["counterfactual"], 4),
        }
        if breached and agg["n"] >= 50:
            threshold_breaches.append({
                "category": cat,
                "ratio": round(ratio, 4),
                "threshold": threshold,
                "sample_size": int(agg["n"]),
            })

    # By wallet top/bottom (only with ≥10 valid trades each, ratio-based)
    wallet_buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"bot_pnl": 0.0, "counterfactual": 0.0, "n": 0}
    )
    for rec in valid_records:
        if not rec.wallet_address:
            continue
        wb = wallet_buckets[rec.wallet_address]
        wb["bot_pnl"] += rec.bot_pnl
        wb["counterfactual"] += rec.source_counterfactual
        wb["n"] += 1
    wallet_ratios: list[dict[str, Any]] = []
    for w, agg in wallet_buckets.items():
        if agg["n"] < 10 or agg["counterfactual"] <= 0:
            continue
        wallet_ratios.append({
            "wallet": w,
            "ratio": round(agg["bot_pnl"] / agg["counterfactual"], 4),
            "n": int(agg["n"]),
        })
    wallet_ratios.sort(key=lambda d: d["ratio"], reverse=True)
    by_wallet_top = wallet_ratios[:10]
    by_wallet_bottom = list(reversed(wallet_ratios[-10:])) if len(wallet_ratios) > 10 else []

    return CopyEfficiencyReport(
        window_hours=window_hours,
        sample_size=sample_size,
        sample_size_excluded=excluded,
        global_ratio=global_ratio,
        bot_pnl_total=round(bot_pnl_total, 4),
        source_counterfactual_total=round(counterfactual_total, 4),
        bot_outperforms_source_count=bot_outperforms,
        by_category=by_category,
        by_wallet_top=by_wallet_top,
        by_wallet_bottom=by_wallet_bottom,
        threshold_breaches=threshold_breaches,
        classification=_classify_global(
            global_ratio=global_ratio, sample_size=len(valid_records)
        ),
    )


def copy_efficiency_payload(
    session: Session, *, window: str = "24h",
) -> dict[str, Any]:
    """FastAPI route convenience wrapper.

    Accepts shorthand windows (24h, 7d, 30d) or numeric hours via the
    ``window`` query parameter."""
    if window in WINDOW_PRESETS_H:
        hours = WINDOW_PRESETS_H[window]
    else:
        try:
            hours = float(window)
        except (TypeError, ValueError):
            hours = 24.0
    return asdict(compute_copy_efficiency_report(session, window_hours=hours))
