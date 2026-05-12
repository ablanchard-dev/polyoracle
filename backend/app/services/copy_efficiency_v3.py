"""M1 v3 — Copy efficiency forensic-grade (review Round 6, 2026-05-12).

Replaces v2 simplifications with rigorous split metrics + correct SELL handling.

v2 problems (now retired):
- `treat SELL as BUY for simplicity` → gonfled ratio to 10.03 (suspicious)
- Single `global_ratio` metric → no way to separate join quality from PnL quality
- No flagging of `same_outcome` mismatches → silent miscount

v3 splits the single ratio into 5 distinct measurements:

1. **join_quality_score** = % of bot trades correctly joined to a source PublicTrade
   (via wallet+market, ideally also same outcome+side). Bad join = unreliable.

2. **side_mapping_quality** = % of joined trades where bot_side == source_side
   AND bot_outcome == source_outcome. Below 95% = pipeline bug.

3. **copy_entry_efficiency** = quality of bot's entry price vs source's entry price.
   For BUY: bot_entry ≤ source_entry + slip_tolerance = good (bot bought cheaper).
   For SELL: bot_entry ≥ source_entry - slip_tolerance = good (bot sold higher).

4. **copy_resolved_efficiency** = bot PnL / source counterfactual PnL,
   ASSUMING source also held to resolution (= same exit strategy).
   For BUY: source_pnl = settlement - entry (long).
   For SELL exit: EXCLUDE (source closed, bot opens new SHORT — non comparable).
   For SELL short open: source_pnl = entry - settlement (short).

5. **source_realized_efficiency** = bot PnL / source's ACTUAL realized PnL
   (= what the wallet really made via their own exit strategy).
   Diagnostic only — NOT a gate metric (wallet exits may differ from bot exits).

Gates for live (per review Round 6):
- join_quality_score ≥ 0.95
- side_mapping_quality ≥ 0.95
- copy_entry_efficiency ≥ 0.70
- copy_resolved_efficiency ≥ 0.70
- ambiguous_source_side ≤ 5%

M1 v3 status flag: `M1_V2_FORENSIC_FIX_NOT_FINAL` until validated on production sample
post-fix. Live trading remains BLOCKED until M1 v3 gates pass.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlmodel import Session, select

from app.models.trade import PaperTrade, PublicTrade
from app.models.wallet import ResolvedMarketRecord
from app.services.fee_engine import compute_dynamic_fee

GLOBAL_MIN_THRESHOLD = 0.70
EXCELLENCE_THRESHOLD = 0.90

# Status flag — MUST stay non-final until 3 ratios validated
M1_STATUS = "M1_V3_FORENSIC_REWRITE"

JoinMethod = Literal[
    "exact_match",         # wallet + market + side + outcome all match
    "partial_match",       # wallet + market match, side or outcome differ
    "wallet_market_only",  # only wallet + market match
    "no_match",            # no source trade found
]

SourceSideClass = Literal[
    "BUY_ENTRY",           # source opened a long position
    "SELL_EXIT",           # source closed a long position (excluded from copy_entry)
    "SELL_SHORT_OPEN",     # source opened a short position (rare)
    "AMBIGUOUS",           # cannot determine context
]


@dataclass
class TradeForensicRecord:
    paper_trade_id: str
    market_id: str
    source_wallet: str | None
    source_trade_id: str | None
    join_method: str
    bot_side: str
    source_side: str | None
    bot_outcome: str
    source_outcome: str | None
    resolved_winner: str | None
    bot_entry_price: float
    source_entry_price: float | None
    bot_exit_price: float | None
    bot_pnl: float
    bot_notional: float
    bot_pnl_per_dollar: float
    source_counterfactual_pnl_per_dollar: float | None
    same_direction: bool
    same_outcome: bool
    sign_match: bool
    source_side_class: str
    excluded_reason: str | None


@dataclass
class M1V3Report:
    status: str
    window_hours: float
    sample_size: int
    # Join quality
    join_quality_score: float           # % exact_match
    join_distribution: dict[str, int]
    side_mapping_quality: float          # % same_direction AND same_outcome
    # Copy entry efficiency (best metric — entry price quality)
    copy_entry_efficiency: float | None
    copy_entry_sample: int
    # Bot vs source resolved comparison (additive metrics — ratios unsafe due
    # to near-zero source PnL/$ from late entries — see 2026-05-12 forensic)
    edge_per_share_avg: float | None     # mean(bot_unit - source_unit), per-share
    edge_per_share_median: float | None
    bot_outperforms_pct: float | None    # % trades where bot_pnl/$ > source_pnl/$
    bot_outperforms_source_count: int
    copy_resolved_sample: int
    bot_pnl_per_dollar_sum: float
    source_pnl_per_dollar_sum: float
    # Side class breakdown
    source_side_class_distribution: dict[str, int]
    ambiguous_source_side_rate: float
    # Bot PnL
    bot_pnl_total: float
    # Alerts (gating violations)
    alerts: list[str]
    classification: str
    forensic_csv_path: str | None = None
    notes: list[str] = field(default_factory=list)


# ---------------- Helpers ----------------


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _resolve_market_record(session: Session, market_id: str | None) -> ResolvedMarketRecord | None:
    """Same fallback as paper_trading_engine: try by primary key then condition_id."""
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


def _extract_bot_settlement(bot_trade: PaperTrade) -> float | None:
    """Bot's actual exit_price at close. Source-of-truth = bot's own metadata."""
    if not bot_trade.close_reason or "|" not in bot_trade.close_reason:
        return None
    try:
        _reason, payload = bot_trade.close_reason.split("|", 1)
        meta = json.loads(payload)
        ex = meta.get("exit_price")
        return float(ex) if ex is not None else None
    except Exception:
        return None


def _winner_outcome_for(session: Session, bot_trade: PaperTrade) -> str | None:
    """Try RMR first, else infer from bot's exit_price + bot's outcome:
    exit_price=1.0 means bot.outcome won; exit_price=0.0 means it lost."""
    rmr = _resolve_market_record(session, bot_trade.market_id)
    if rmr and rmr.winning_outcome_name:
        return rmr.winning_outcome_name.strip()
    exit_price = _extract_bot_settlement(bot_trade)
    if exit_price is None:
        return None
    bot_outcome = (bot_trade.outcome or "").strip()
    if not bot_outcome:
        return None
    if exit_price >= 0.95:
        return bot_outcome  # bot's outcome won
    if exit_price <= 0.05:
        # Bot's outcome lost. Cannot deterministically name the winning side
        # without knowing the market's other outcome label. Return "<not_bot_outcome>"
        # as a marker.
        return f"<not_{bot_outcome}>"
    # Mid-price = manual close, not a resolution
    return None


def _classify_source_side(
    *, source_trade: PublicTrade, session: Session, before_ts: datetime,
) -> SourceSideClass:
    """Determine if source's SELL is an exit, a short-open, or ambiguous.

    Heuristic:
    - If source's side == BUY: always BUY_ENTRY.
    - If source's side == SELL: look at prior trades on same wallet+market.
      If wallet has prior BUYs > prior SELLs on this market → SELL_EXIT (close long).
      If wallet has 0 prior BUYs but some prior SELLs → SELL_SHORT_OPEN or repeated exits.
      If unclear → AMBIGUOUS.
    """
    side = (source_trade.side or "").upper()
    if side == "BUY":
        return "BUY_ENTRY"
    if side != "SELL":
        return "AMBIGUOUS"
    # SELL case — count prior actions on this wallet+market
    wallet = source_trade.wallet_address
    market = source_trade.market_id
    if not wallet or not market:
        return "AMBIGUOUS"
    try:
        prior_buys = session.exec(
            select(PublicTrade)
            .where(PublicTrade.wallet_address == wallet)
            .where(PublicTrade.market_id == market)
            .where(PublicTrade.side == "BUY")
        ).all()
        prior_buy_count = sum(
            1 for t in prior_buys
            if (_ensure_aware(t.traded_at) or before_ts) < before_ts
        )
    except Exception:
        prior_buy_count = 0
    if prior_buy_count > 0:
        return "SELL_EXIT"
    return "SELL_SHORT_OPEN"


def _compute_source_counterfactual_pnl_per_dollar(
    *,
    side_class: SourceSideClass,
    source_entry_price: float,
    source_outcome: str | None,
    winner_outcome: str | None,
    bot_settlement: float,
    category: str,
) -> tuple[float | None, str | None]:
    """Compute source's PnL-per-dollar IF they had held to resolution.

    Returns (pnl_per_dollar, exclusion_reason). pnl_per_dollar=None means
    excluded from copy_resolved_efficiency calc.

    Rules per review Round 6:
    - BUY_ENTRY: long position, pnl = (settlement - entry) per share.
    - SELL_SHORT_OPEN: short position, pnl = (entry - settlement) per share.
    - SELL_EXIT: EXCLUDE (source closed, bot opened new exposure — non comparable).
    - AMBIGUOUS: EXCLUDE.
    """
    if side_class == "SELL_EXIT":
        return (None, "source_was_exit_not_entry")
    if side_class == "AMBIGUOUS":
        return (None, "source_side_ambiguous")
    if source_entry_price <= 0 or source_entry_price >= 1:
        return (None, "invalid_source_entry_price")
    if winner_outcome is None:
        return (None, "no_winner_determinable")

    # Determine if source's outcome won
    src_won: bool
    if winner_outcome.startswith("<not_"):
        # Marker: bot's outcome lost. Source won iff source.outcome ≠ bot.outcome.
        bot_outcome = winner_outcome[len("<not_"):-1]
        src_won = (source_outcome or "").lower() != bot_outcome.lower()
    else:
        src_won = (source_outcome or "").strip().lower() == winner_outcome.strip().lower()

    # Per-share settlement for source's position
    settle = 1.0 if src_won else 0.0
    fee_per_share = compute_dynamic_fee(category, source_entry_price, 1.0)
    if side_class == "BUY_ENTRY":
        unit_pnl = settle - source_entry_price - fee_per_share
    else:  # SELL_SHORT_OPEN
        unit_pnl = source_entry_price - settle - fee_per_share
    # Normalize to per-dollar: divide by entry price (= cost basis for BUY) or (1 - entry) for short
    if side_class == "BUY_ENTRY":
        pnl_per_dollar = unit_pnl / source_entry_price
    else:
        pnl_per_dollar = unit_pnl / (1.0 - source_entry_price)
    return (pnl_per_dollar, None)


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


# ---------------- Main entrypoint ----------------


def build_forensic_records(
    session: Session, *, window_hours: float = 24.0, baseline_t0: datetime | None = None,
) -> list[TradeForensicRecord]:
    """Build per-trade forensic records. Pure function — no mutation."""
    cutoff = baseline_t0 or (datetime.now(UTC) - timedelta(hours=window_hours))
    closed_trades = session.exec(
        select(PaperTrade).where(PaperTrade.status == "closed")
    ).all()
    records: list[TradeForensicRecord] = []
    for trade in closed_trades:
        closed = _ensure_aware(trade.closed_at)
        if closed is None or closed < cutoff:
            continue
        bot_side = (trade.side or "").upper()
        bot_outcome = (trade.outcome or "")
        bot_pnl = float(trade.realized_pnl or 0.0)
        bot_notional = float(trade.notional_usd or 0.0)
        bot_entry = float(trade.average_price or 0.0)
        bot_exit = _extract_bot_settlement(trade)
        category = _category_of(trade.market_id, session)
        winner = _winner_outcome_for(session, trade)
        bot_pnl_per_dollar = (bot_pnl / bot_notional) if bot_notional > 0 else 0.0

        # Source PublicTrade lookup
        source_trade: PublicTrade | None = None
        join_method: str = "no_match"
        if trade.wallet_address and trade.market_id:
            candidates = list(session.exec(
                select(PublicTrade)
                .where(PublicTrade.wallet_address == trade.wallet_address)
                .where(PublicTrade.market_id == trade.market_id)
            ).all())
            if candidates:
                # Prefer exact-match by side + outcome
                exact = [
                    r for r in candidates
                    if (r.side or "").upper() == bot_side
                    and (r.outcome or "").lower() == bot_outcome.lower()
                ]
                if exact:
                    source_trade = sorted(
                        exact,
                        key=lambda r: abs(
                            ((_ensure_aware(r.traded_at) or _ensure_aware(trade.opened_at) or datetime.now(UTC))
                             - (_ensure_aware(trade.opened_at) or datetime.now(UTC))).total_seconds()
                        ),
                    )[0]
                    join_method = "exact_match"
                else:
                    # Partial: same wallet+market but different side or outcome
                    source_trade = sorted(
                        candidates,
                        key=lambda r: abs(
                            ((_ensure_aware(r.traded_at) or _ensure_aware(trade.opened_at) or datetime.now(UTC))
                             - (_ensure_aware(trade.opened_at) or datetime.now(UTC))).total_seconds()
                        ),
                    )[0]
                    join_method = (
                        "partial_match"
                        if (source_trade.side or "").upper() == bot_side
                        or (source_trade.outcome or "").lower() == bot_outcome.lower()
                        else "wallet_market_only"
                    )

        src_side = (source_trade.side or "").upper() if source_trade else None
        src_outcome = (source_trade.outcome or "") if source_trade else None
        src_entry = float(source_trade.price or 0.0) if source_trade else None

        same_direction = src_side == bot_side if src_side else False
        same_outcome = (
            src_outcome and bot_outcome and src_outcome.lower() == bot_outcome.lower()
        ) if src_outcome else False

        # Classify source side context
        side_class: str = "AMBIGUOUS"
        if source_trade:
            side_class = _classify_source_side(
                source_trade=source_trade,
                session=session,
                before_ts=_ensure_aware(trade.opened_at) or datetime.now(UTC),
            )

        # Source counterfactual PnL per dollar (resolved-hold scenario)
        source_pnl_per_dollar: float | None = None
        excluded: str | None = None
        if source_trade and src_entry is not None and bot_exit is not None:
            source_pnl_per_dollar, excluded = _compute_source_counterfactual_pnl_per_dollar(
                side_class=side_class,
                source_entry_price=src_entry,
                source_outcome=src_outcome,
                winner_outcome=winner,
                bot_settlement=bot_exit,
                category=category,
            )
        elif not source_trade:
            excluded = "no_source_trade"
        else:
            excluded = "missing_data"

        sign_match = (
            (bot_pnl > 0 and source_pnl_per_dollar is not None and source_pnl_per_dollar > 0)
            or (bot_pnl < 0 and source_pnl_per_dollar is not None and source_pnl_per_dollar < 0)
        )

        records.append(TradeForensicRecord(
            paper_trade_id=trade.id,
            market_id=trade.market_id,
            source_wallet=trade.wallet_address,
            source_trade_id=source_trade.id if source_trade else None,
            join_method=join_method,
            bot_side=bot_side,
            source_side=src_side,
            bot_outcome=bot_outcome,
            source_outcome=src_outcome,
            resolved_winner=winner,
            bot_entry_price=bot_entry,
            source_entry_price=src_entry,
            bot_exit_price=bot_exit,
            bot_pnl=bot_pnl,
            bot_notional=bot_notional,
            bot_pnl_per_dollar=round(bot_pnl_per_dollar, 6),
            source_counterfactual_pnl_per_dollar=(
                round(source_pnl_per_dollar, 6) if source_pnl_per_dollar is not None else None
            ),
            same_direction=same_direction,
            same_outcome=same_outcome,
            sign_match=sign_match,
            source_side_class=side_class,
            excluded_reason=excluded,
        ))
    return records


def compute_m1_v3_report(
    session: Session, *, window_hours: float = 24.0, baseline_t0: datetime | None = None,
    write_csv: bool = False,
) -> M1V3Report:
    records = build_forensic_records(session, window_hours=window_hours, baseline_t0=baseline_t0)
    sample = len(records)
    if sample == 0:
        return M1V3Report(
            status=M1_STATUS, window_hours=window_hours, sample_size=0,
            join_quality_score=0.0, join_distribution={},
            side_mapping_quality=0.0,
            copy_entry_efficiency=None, copy_entry_sample=0,
            edge_per_share_avg=None, edge_per_share_median=None,
            bot_outperforms_pct=None, bot_outperforms_source_count=0,
            copy_resolved_sample=0,
            bot_pnl_per_dollar_sum=0.0, source_pnl_per_dollar_sum=0.0,
            source_side_class_distribution={},
            ambiguous_source_side_rate=0.0,
            bot_pnl_total=0.0,
            alerts=["empty_sample"],
            classification="UNKNOWN",
        )

    # Join quality
    join_dist = Counter(r.join_method for r in records)
    exact_count = join_dist.get("exact_match", 0)
    join_quality = exact_count / sample

    # Side mapping quality (= % same_direction AND same_outcome among joined trades)
    joined = [r for r in records if r.join_method != "no_match"]
    if joined:
        side_mapping_quality = sum(
            1 for r in joined if r.same_direction and r.same_outcome
        ) / len(joined)
    else:
        side_mapping_quality = 0.0

    # Source side class distribution
    side_class_dist = Counter(r.source_side_class for r in records)
    ambiguous_rate = side_class_dist.get("AMBIGUOUS", 0) / sample

    # Copy entry efficiency: for BUY entries, bot should buy at <= source's price
    # Score = % of records where bot_entry within tolerance of source_entry
    entry_eligible = [
        r for r in records
        if r.source_side_class in ("BUY_ENTRY", "SELL_SHORT_OPEN")
        and r.source_entry_price is not None
        and r.bot_entry_price > 0
        and r.same_direction
        and r.same_outcome
    ]
    if entry_eligible:
        slip_tolerance = 0.02
        good_entries = 0
        for r in entry_eligible:
            if r.bot_side == "BUY":
                if r.bot_entry_price <= r.source_entry_price + slip_tolerance:
                    good_entries += 1
            else:  # SELL short open
                if r.bot_entry_price >= r.source_entry_price - slip_tolerance:
                    good_entries += 1
        copy_entry_efficiency = good_entries / len(entry_eligible)
        copy_entry_sample = len(entry_eligible)
    else:
        copy_entry_efficiency = None
        copy_entry_sample = 0

    # Resolved comparison — ADDITIVE metrics, no ratio (forensic 2026-05-12
    # showed source counterfactual PnL/$ is net negative because smart wallets
    # enter at prices near settlement: small winning gain per $, full loss on miss.
    # A bot/source ratio would be unbounded and misleading. Instead:
    # - edge_per_share = (bot_unit_pnl - source_unit_pnl) per share. >0 = bot beats source.
    # - bot_outperforms_pct = % trades where bot PnL/$ > source PnL/$.
    resolved_eligible = [
        r for r in records
        if r.source_counterfactual_pnl_per_dollar is not None
    ]

    def _unit_pnl(pnl_per_dollar: float, entry_price: float, side: str) -> float:
        """Convert per-dollar PnL back to per-share unit. For BUY: cost-basis=entry;
        for SELL short: cost-basis = (1 - entry)."""
        cost = entry_price if side == "BUY" else (1.0 - entry_price)
        return pnl_per_dollar * cost

    edge_per_share_list: list[float] = []
    bot_pnl_per_dollar_sum = 0.0
    source_pnl_per_dollar_sum = 0.0
    bot_outperforms = 0
    for r in resolved_eligible:
        src_unit = _unit_pnl(
            r.source_counterfactual_pnl_per_dollar or 0.0,
            r.source_entry_price or 0.0,
            r.bot_side,
        )
        bot_unit = _unit_pnl(
            r.bot_pnl_per_dollar,
            r.bot_entry_price,
            r.bot_side,
        )
        edge_per_share_list.append(bot_unit - src_unit)
        bot_pnl_per_dollar_sum += r.bot_pnl_per_dollar
        source_pnl_per_dollar_sum += r.source_counterfactual_pnl_per_dollar or 0.0
        if r.bot_pnl_per_dollar > (r.source_counterfactual_pnl_per_dollar or 0.0):
            bot_outperforms += 1

    if edge_per_share_list:
        sorted_edges = sorted(edge_per_share_list)
        mid = len(sorted_edges) // 2
        if len(sorted_edges) % 2:
            edge_per_share_median = sorted_edges[mid]
        else:
            edge_per_share_median = (sorted_edges[mid - 1] + sorted_edges[mid]) / 2
        edge_per_share_avg = sum(edge_per_share_list) / len(edge_per_share_list)
        bot_outperforms_pct = bot_outperforms / len(resolved_eligible)
    else:
        edge_per_share_avg = None
        edge_per_share_median = None
        bot_outperforms_pct = None

    # Bot PnL total
    bot_pnl_total = sum(r.bot_pnl for r in records)

    # Alerts (live gates per review Round 6, recalibrated 2026-05-12 for additive metrics)
    alerts: list[str] = []
    if join_quality < 0.95:
        alerts.append(f"join_quality_score {join_quality:.2f} < 0.95")
    if side_mapping_quality < 0.95:
        alerts.append(f"side_mapping_quality {side_mapping_quality:.2f} < 0.95")
    if copy_entry_efficiency is not None and copy_entry_efficiency < 0.70:
        alerts.append(f"copy_entry_efficiency {copy_entry_efficiency:.2f} < 0.70")
    if edge_per_share_avg is not None and edge_per_share_avg <= 0:
        alerts.append(f"edge_per_share_avg {edge_per_share_avg:.4f} ≤ 0 (bot not beating source)")
    if bot_outperforms_pct is not None and bot_outperforms_pct < 0.60:
        alerts.append(f"bot_outperforms_pct {bot_outperforms_pct:.2%} < 60%")
    if ambiguous_rate > 0.05:
        alerts.append(f"ambiguous_source_side_rate {ambiguous_rate:.2%} > 5%")

    if alerts:
        classification = "WARN_LIVE_BLOCKED"
    elif (
        edge_per_share_avg is not None
        and edge_per_share_avg > 0
        and bot_outperforms_pct is not None
        and bot_outperforms_pct >= 0.70
        and copy_entry_efficiency is not None
        and copy_entry_efficiency >= 0.80
    ):
        classification = "HEALTHY_LIVE_READY_PENDING_VALIDATION"
    else:
        classification = "INSUFFICIENT_DATA"

    csv_path: str | None = None
    if write_csv:
        from pathlib import Path
        import csv as _csv
        out = Path("/opt/app/polyoracle/data/exports") / f"m1_v3_forensic_{datetime.now(UTC).strftime('%Y%m%dT%H%MZ')}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
            writer.writeheader()
            for r in records:
                writer.writerow(asdict(r))
        csv_path = str(out)

    return M1V3Report(
        status=M1_STATUS,
        window_hours=window_hours,
        sample_size=sample,
        join_quality_score=round(join_quality, 4),
        join_distribution=dict(join_dist),
        side_mapping_quality=round(side_mapping_quality, 4),
        copy_entry_efficiency=round(copy_entry_efficiency, 4) if copy_entry_efficiency is not None else None,
        copy_entry_sample=copy_entry_sample,
        edge_per_share_avg=round(edge_per_share_avg, 6) if edge_per_share_avg is not None else None,
        edge_per_share_median=round(edge_per_share_median, 6) if edge_per_share_median is not None else None,
        bot_outperforms_pct=round(bot_outperforms_pct, 4) if bot_outperforms_pct is not None else None,
        bot_outperforms_source_count=bot_outperforms,
        copy_resolved_sample=len(resolved_eligible),
        bot_pnl_per_dollar_sum=round(bot_pnl_per_dollar_sum, 4),
        source_pnl_per_dollar_sum=round(source_pnl_per_dollar_sum, 4),
        source_side_class_distribution=dict(side_class_dist),
        ambiguous_source_side_rate=round(ambiguous_rate, 4),
        bot_pnl_total=round(bot_pnl_total, 4),
        alerts=alerts,
        classification=classification,
        forensic_csv_path=csv_path,
    )


def m1_v3_payload(
    session: Session, *, window_hours: float = 24.0, write_csv: bool = False,
) -> dict[str, Any]:
    """For /edge/copy-efficiency-v3 endpoint."""
    from app.services.baseline_constants import EFFECTIVE_BASELINE_T0
    return asdict(compute_m1_v3_report(
        session, window_hours=window_hours,
        baseline_t0=EFFECTIVE_BASELINE_T0, write_csv=write_csv,
    ))
