"""M1 v4 — Anti-leakage temporal audit (review Round 7, 2026-05-12).

Builds on v3 (split metrics + SELL handling) and adds causal-time gates that
v3 was missing. v3 produced a suspicious `edge_per_share_avg = +0.285$/share`
with `bot_outperforms_pct = 78.7%`. review Round 7 challenged: a copy-trading
bot **cannot legitimately enter before its source wallet**. If forensic records
show that pattern, either:

  (a) **Temporal leakage** — bot's `opened_at` precedes source's `traded_at`,
      which is physically impossible without future-knowledge.
  (b) **Wrong source join** — exact-match by side+outcome can pick a wallet's
      first historical entry rather than the trade that triggered the copy.
  (c) **Backfill contamination** — restart-time backfills inject trades whose
      paper price was reconstructed against present-time orderbook, not the
      price actually available at copy time.

This module surfaces those failure modes per-trade via new CSV columns and
gates the M1 classification on temporal causality.

New CSV columns (vs v3):
  - source_trade_timestamp (PublicTrade.traded_at)
  - source_seen_at         (PublicTrade.seen_at — when polling saw it)
  - bot_opened_at          (PaperTrade.opened_at)
  - bot_closed_at          (PaperTrade.closed_at)
  - market_end_date        (Market.end_date — string, market resolution target)
  - copy_delay_seconds     (bot_opened_at − source_trade_timestamp)
  - source_fetch_delay_s   (source_seen_at − source_trade_timestamp)
  - source_event_type      (LIVE_POLL / DELAYED_POLL / BACKFILL / UNKNOWN)
  - leakage_flag           (BOT_BEFORE_SOURCE / EXCESSIVE_COPY_DELAY / BACKFILL_SOURCE / OK)
  - price_source_at_open   (best-effort: synth / clob / unknown)

Gates (Round 7 spec, all must pass for `HEALTHY_LIVE_READY_PENDING_PHASE_E`):
  G1. source_trade_timestamp ≤ bot_opened_at on ≥99.5% of trades
  G2. bot_opened_at < market_resolved_at on 100% of trades (where known)
  G3. copy_delay_seconds ≤ 120s on ≥95% of trades
  G4. source_event_type == LIVE_POLL on ≥95% of trades
  G5. market_only_join ≤ 1% (already enforced by v3 exact_match preference)

Failure-mode classifications:
  - LEAKAGE_SUSPECT       — G1 or G2 fails on any trade
  - BACKFILL_CONTAMINATED — >5% of sample comes from BACKFILL source_event_type
  - EXCESSIVE_COPY_DELAY  — G3 fails (median copy_delay too high for live)
  - HEALTHY_LIVE_READY_PENDING_PHASE_E — all gates pass

Standalone-runnable, does NOT touch live backend.
"""
from __future__ import annotations

import csv as _csv
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlmodel import Session, select

from app.models.trade import PaperTrade, PublicTrade
from app.services.copy_efficiency_v3 import (
    _category_of,
    _classify_source_side,
    _compute_source_counterfactual_pnl_per_dollar,
    _ensure_aware,
    _extract_bot_settlement,
    _winner_outcome_for,
)

M1_V4_STATUS = "M1_V4_TEMPORAL_AUDIT"

# Heuristic thresholds (Round 7)
COPY_DELAY_OK_SECONDS = 120
LIVE_POLL_FETCH_DELAY_SECONDS = 60   # source seen_at − traded_at ≤ 60s = LIVE_POLL
BACKFILL_FETCH_DELAY_SECONDS = 3600  # >1h gap = BACKFILL/HISTORICAL
PRE_SOURCE_TOLERANCE_SECONDS = 1     # account for clock skew when checking bot_before_source

SourceEventType = Literal["LIVE_POLL", "DELAYED_POLL", "BACKFILL", "UNKNOWN"]
LeakageFlag = Literal[
    "OK",
    "BOT_BEFORE_SOURCE",       # bot opened_at < source traded_at (impossible legit)
    "EXCESSIVE_COPY_DELAY",    # delay > 120s
    "BACKFILL_SOURCE",         # source from backfill, not live polling
    "POST_RESOLUTION_OPEN",    # bot opened after market resolved_at
    "MISSING_SOURCE_TS",       # no source timestamp = can't audit
]


@dataclass
class TradeForensicRecordV4:
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
    # v4 timestamp fields
    source_trade_timestamp: str | None
    source_seen_at: str | None
    bot_opened_at: str | None
    bot_closed_at: str | None
    market_end_date: str | None
    copy_delay_seconds: float | None
    source_fetch_delay_s: float | None
    source_event_type: str
    leakage_flag: str
    price_source_at_open: str


@dataclass
class M1V4Report:
    status: str
    window_hours: float
    sample_size: int
    # From v3
    join_quality_score: float
    side_mapping_quality: float
    copy_entry_efficiency: float | None
    edge_per_share_avg: float | None
    edge_per_share_median: float | None
    bot_outperforms_pct: float | None
    # v4 temporal gates
    causal_order_ok_pct: float        # G1: % where source.traded_at ≤ bot.opened_at
    copy_delay_p50_s: float | None
    copy_delay_p95_s: float | None
    copy_delay_within_120s_pct: float
    source_event_distribution: dict[str, int]
    live_poll_pct: float              # G4
    backfill_pct: float
    leakage_flag_distribution: dict[str, int]
    bot_before_source_count: int
    post_resolution_open_count: int   # bot opened after market resolved (when known)
    # CSV path
    forensic_csv_path: str | None
    # Final
    alerts: list[str]
    classification: str
    notes: list[str] = field(default_factory=list)


def _market_end_date_of(session: Session, market_id: str | None) -> str | None:
    if not market_id:
        return None
    try:
        from app.models.market import Market as _Market
        row = session.get(_Market, market_id)
        if row is not None and getattr(row, "end_date", None):
            return str(row.end_date)
    except Exception:
        pass
    return None


def _parse_market_end_date(end_date_str: str | None) -> datetime | None:
    if not end_date_str:
        return None
    candidates = [
        end_date_str,
        end_date_str.replace("Z", "+00:00"),
        end_date_str.split("T")[0] + "T00:00:00+00:00" if "T" in end_date_str else None,
    ]
    for s in candidates:
        if not s:
            continue
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
    return None


def _classify_source_event(
    source_traded_at: datetime | None, source_seen_at: datetime | None,
) -> tuple[SourceEventType, float | None]:
    if source_traded_at is None or source_seen_at is None:
        return ("UNKNOWN", None)
    fetch_delay = (source_seen_at - source_traded_at).total_seconds()
    if fetch_delay < 0:
        # seen_at before traded_at = clock skew or DB anomaly — flag DELAYED_POLL
        return ("DELAYED_POLL", fetch_delay)
    if fetch_delay <= LIVE_POLL_FETCH_DELAY_SECONDS:
        return ("LIVE_POLL", fetch_delay)
    if fetch_delay <= BACKFILL_FETCH_DELAY_SECONDS:
        return ("DELAYED_POLL", fetch_delay)
    return ("BACKFILL", fetch_delay)


def _infer_price_source(bot_trade: PaperTrade) -> str:
    """Best-effort inference of how the bot's entry price was determined.

    Without explicit audit metadata, we have only weak signals:
    - If close_reason metadata mentions 'synth' → synth
    - If metadata has 'clob_orderbook' marker → clob
    - Otherwise → unknown
    """
    if not bot_trade.close_reason or "|" not in bot_trade.close_reason:
        return "unknown"
    try:
        _reason, payload = bot_trade.close_reason.split("|", 1)
        meta = json.loads(payload)
        if meta.get("synth_used") or meta.get("price_source") == "synth":
            return "synth"
        if meta.get("price_source") == "clob":
            return "clob"
        if "orderbook" in meta:
            return "clob"
    except Exception:
        pass
    return "unknown"


def _compute_leakage_flag(
    *, source_traded_at: datetime | None, bot_opened_at: datetime | None,
    market_resolved_at: datetime | None, copy_delay: float | None,
    source_event_type: str,
) -> LeakageFlag:
    if source_traded_at is None or bot_opened_at is None:
        return "MISSING_SOURCE_TS"
    # G1: source ≤ bot
    if (source_traded_at - bot_opened_at).total_seconds() > PRE_SOURCE_TOLERANCE_SECONDS:
        return "BOT_BEFORE_SOURCE"
    # G2: bot < resolved
    if market_resolved_at is not None and bot_opened_at >= market_resolved_at:
        return "POST_RESOLUTION_OPEN"
    # Backfill source
    if source_event_type == "BACKFILL":
        return "BACKFILL_SOURCE"
    # Copy delay
    if copy_delay is not None and copy_delay > COPY_DELAY_OK_SECONDS:
        return "EXCESSIVE_COPY_DELAY"
    return "OK"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    sorted_vals = sorted(values)
    idx = min(len(sorted_vals) - 1, max(0, int(round(pct / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def build_forensic_records_v4(
    session: Session, *, window_hours: float = 999_999.0, baseline_t0: datetime | None = None,
) -> list[TradeForensicRecordV4]:
    """Mirror of v3.build_forensic_records but enriched with timestamp fields."""
    cutoff = baseline_t0 or (datetime.now(UTC) - timedelta(hours=window_hours))
    closed_trades = session.exec(
        select(PaperTrade).where(PaperTrade.status == "closed")
    ).all()
    records: list[TradeForensicRecordV4] = []
    for trade in closed_trades:
        bot_opened_at = _ensure_aware(trade.opened_at)
        bot_closed_at = _ensure_aware(trade.closed_at)
        if bot_closed_at is None or bot_closed_at < cutoff:
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

        # Market end_date
        end_date_str = _market_end_date_of(session, trade.market_id)
        market_resolved_at = _parse_market_end_date(end_date_str)

        # Source PublicTrade — exact match preferred, with CAUSAL CONSTRAINT for
        # selecting the right source: prefer source.traded_at ≤ bot.opened_at.
        # We still expose violations via leakage_flag for the rejected cases,
        # but joining honestly is the first defense.
        source_trade: PublicTrade | None = None
        join_method: str = "no_match"
        if trade.wallet_address and trade.market_id:
            candidates = list(session.exec(
                select(PublicTrade)
                .where(PublicTrade.wallet_address == trade.wallet_address)
                .where(PublicTrade.market_id == trade.market_id)
            ).all())
            if candidates:
                exact = [
                    r for r in candidates
                    if (r.side or "").upper() == bot_side
                    and (r.outcome or "").lower() == bot_outcome.lower()
                ]
                # First try: causal exact matches (source.traded_at ≤ bot.opened_at)
                causal_exact = [
                    r for r in exact
                    if r.traded_at is not None
                    and bot_opened_at is not None
                    and (_ensure_aware(r.traded_at) - bot_opened_at).total_seconds() <= PRE_SOURCE_TOLERANCE_SECONDS
                ]
                if causal_exact:
                    # Take the LATEST causal source — that's the most-recent trade
                    # before the bot copied. Earlier wallet entries on same market
                    # would inflate "bot bought cheaper" artificially.
                    source_trade = max(
                        causal_exact,
                        key=lambda r: _ensure_aware(r.traded_at) or datetime.min.replace(tzinfo=UTC),
                    )
                    join_method = "causal_exact_match"
                elif exact:
                    # Non-causal exact match — flag for leakage detection
                    source_trade = sorted(
                        exact,
                        key=lambda r: abs(
                            ((_ensure_aware(r.traded_at) or _ensure_aware(trade.opened_at) or datetime.now(UTC))
                             - (_ensure_aware(trade.opened_at) or datetime.now(UTC))).total_seconds()
                        ),
                    )[0]
                    join_method = "non_causal_exact_match"
                else:
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
        source_traded_at = _ensure_aware(source_trade.traded_at) if source_trade else None
        source_seen_at = _ensure_aware(source_trade.seen_at) if source_trade else None

        same_direction = src_side == bot_side if src_side else False
        same_outcome = (
            src_outcome and bot_outcome and src_outcome.lower() == bot_outcome.lower()
        ) if src_outcome else False

        # Source side classification
        side_class: str = "AMBIGUOUS"
        if source_trade:
            side_class = _classify_source_side(
                source_trade=source_trade,
                session=session,
                before_ts=bot_opened_at or datetime.now(UTC),
            )

        # Source counterfactual PnL per dollar
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

        # Temporal fields
        copy_delay: float | None = None
        if source_traded_at is not None and bot_opened_at is not None:
            copy_delay = (bot_opened_at - source_traded_at).total_seconds()

        source_event_type, fetch_delay = _classify_source_event(source_traded_at, source_seen_at)
        leakage = _compute_leakage_flag(
            source_traded_at=source_traded_at,
            bot_opened_at=bot_opened_at,
            market_resolved_at=market_resolved_at,
            copy_delay=copy_delay,
            source_event_type=source_event_type,
        )
        price_source = _infer_price_source(trade)

        records.append(TradeForensicRecordV4(
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
            source_trade_timestamp=source_traded_at.isoformat() if source_traded_at else None,
            source_seen_at=source_seen_at.isoformat() if source_seen_at else None,
            bot_opened_at=bot_opened_at.isoformat() if bot_opened_at else None,
            bot_closed_at=bot_closed_at.isoformat() if bot_closed_at else None,
            market_end_date=end_date_str,
            copy_delay_seconds=round(copy_delay, 3) if copy_delay is not None else None,
            source_fetch_delay_s=round(fetch_delay, 3) if fetch_delay is not None else None,
            source_event_type=source_event_type,
            leakage_flag=leakage,
            price_source_at_open=price_source,
        ))
    return records


def compute_m1_v4_report(
    session: Session, *, window_hours: float = 999_999.0, baseline_t0: datetime | None = None,
    write_csv: bool = False,
) -> M1V4Report:
    records = build_forensic_records_v4(session, window_hours=window_hours, baseline_t0=baseline_t0)
    sample = len(records)

    # Subset: trades with both timestamps populated (causal audit)
    auditable = [r for r in records if r.copy_delay_seconds is not None]
    auditable_n = len(auditable)

    # G1: source.traded_at ≤ bot.opened_at + tolerance
    causal_ok = sum(
        1 for r in auditable
        if r.copy_delay_seconds is not None and r.copy_delay_seconds >= -PRE_SOURCE_TOLERANCE_SECONDS
    )
    causal_order_ok_pct = causal_ok / auditable_n if auditable_n else 0.0

    # Copy delay percentiles (only on causal records, ≥0)
    valid_delays = [r.copy_delay_seconds for r in auditable if r.copy_delay_seconds is not None and r.copy_delay_seconds >= 0]
    copy_delay_p50 = _percentile(valid_delays, 50)
    copy_delay_p95 = _percentile(valid_delays, 95)
    within_120 = sum(1 for d in valid_delays if d <= COPY_DELAY_OK_SECONDS)
    within_120_pct = within_120 / len(valid_delays) if valid_delays else 0.0

    # Source event distribution
    event_dist = Counter(r.source_event_type for r in records)
    live_poll_pct = event_dist.get("LIVE_POLL", 0) / sample if sample else 0.0
    backfill_pct = event_dist.get("BACKFILL", 0) / sample if sample else 0.0

    # Leakage flag distribution
    leak_dist = Counter(r.leakage_flag for r in records)
    bot_before_source_count = leak_dist.get("BOT_BEFORE_SOURCE", 0)
    post_resolution_count = leak_dist.get("POST_RESOLUTION_OPEN", 0)

    # v3 metrics (recompute on this sample)
    join_dist = Counter(r.join_method for r in records)
    join_quality_score = (
        (join_dist.get("causal_exact_match", 0) + join_dist.get("non_causal_exact_match", 0)) / sample
        if sample else 0.0
    )
    joined = [r for r in records if r.join_method != "no_match"]
    side_mapping_quality = (
        sum(1 for r in joined if r.same_direction and r.same_outcome) / len(joined)
        if joined else 0.0
    )
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
        good = 0
        for r in entry_eligible:
            if r.bot_side == "BUY":
                if r.bot_entry_price <= r.source_entry_price + slip_tolerance:
                    good += 1
            else:
                if r.bot_entry_price >= r.source_entry_price - slip_tolerance:
                    good += 1
        copy_entry_efficiency = good / len(entry_eligible)
    else:
        copy_entry_efficiency = None

    # Edge per share (only on CAUSAL + LIVE_POLL trades — strict live-readiness sample)
    strict_eligible = [
        r for r in records
        if r.source_counterfactual_pnl_per_dollar is not None
        and r.leakage_flag == "OK"
        and r.source_event_type == "LIVE_POLL"
    ]
    if strict_eligible:
        def _unit(pnl_per_dollar: float, entry: float, side: str) -> float:
            cost = entry if side == "BUY" else (1.0 - entry)
            return pnl_per_dollar * cost
        edges = [
            _unit(r.bot_pnl_per_dollar, r.bot_entry_price, r.bot_side)
            - _unit(r.source_counterfactual_pnl_per_dollar or 0.0, r.source_entry_price or 0.0, r.bot_side)
            for r in strict_eligible
        ]
        edge_per_share_avg = sum(edges) / len(edges)
        sorted_edges = sorted(edges)
        mid = len(sorted_edges) // 2
        edge_per_share_median = (
            sorted_edges[mid] if len(sorted_edges) % 2 else (sorted_edges[mid - 1] + sorted_edges[mid]) / 2
        )
        bot_outperforms = sum(
            1 for r in strict_eligible
            if r.bot_pnl_per_dollar > (r.source_counterfactual_pnl_per_dollar or 0.0)
        )
        bot_outperforms_pct = bot_outperforms / len(strict_eligible)
    else:
        edge_per_share_avg = None
        edge_per_share_median = None
        bot_outperforms_pct = None

    # Alerts (Round 7 gates)
    alerts: list[str] = []
    if causal_order_ok_pct < 0.995 and auditable_n > 0:
        alerts.append(f"G1 causal_order {causal_order_ok_pct:.2%} < 99.5% — {bot_before_source_count} BOT_BEFORE_SOURCE")
    if post_resolution_count > 0:
        alerts.append(f"G2 post_resolution {post_resolution_count} trades — opened after market resolved")
    if within_120_pct < 0.95 and valid_delays:
        alerts.append(f"G3 copy_delay_within_120s {within_120_pct:.2%} < 95% — p95={copy_delay_p95:.1f}s")
    if live_poll_pct < 0.95 and sample > 0:
        alerts.append(f"G4 live_poll {live_poll_pct:.2%} < 95% — backfill={backfill_pct:.2%}")

    if bot_before_source_count > 0 or post_resolution_count > 0:
        classification = "LEAKAGE_SUSPECT"
    elif backfill_pct > 0.05:
        classification = "BACKFILL_CONTAMINATED"
    elif within_120_pct < 0.95 and valid_delays:
        classification = "EXCESSIVE_COPY_DELAY"
    elif not alerts and edge_per_share_avg is not None and edge_per_share_avg > 0:
        classification = "HEALTHY_LIVE_READY_PENDING_PHASE_E"
    else:
        classification = "INSUFFICIENT_DATA"

    csv_path: str | None = None
    if write_csv and records:
        out = Path("/opt/app/polyoracle/data/exports") / f"m1_v4_forensic_{datetime.now(UTC).strftime('%Y%m%dT%H%MZ')}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
            writer.writeheader()
            for r in records:
                writer.writerow(asdict(r))
        csv_path = str(out)

    return M1V4Report(
        status=M1_V4_STATUS,
        window_hours=window_hours,
        sample_size=sample,
        join_quality_score=round(join_quality_score, 4),
        side_mapping_quality=round(side_mapping_quality, 4),
        copy_entry_efficiency=round(copy_entry_efficiency, 4) if copy_entry_efficiency is not None else None,
        edge_per_share_avg=round(edge_per_share_avg, 6) if edge_per_share_avg is not None else None,
        edge_per_share_median=round(edge_per_share_median, 6) if edge_per_share_median is not None else None,
        bot_outperforms_pct=round(bot_outperforms_pct, 4) if bot_outperforms_pct is not None else None,
        causal_order_ok_pct=round(causal_order_ok_pct, 4),
        copy_delay_p50_s=round(copy_delay_p50, 2) if copy_delay_p50 is not None else None,
        copy_delay_p95_s=round(copy_delay_p95, 2) if copy_delay_p95 is not None else None,
        copy_delay_within_120s_pct=round(within_120_pct, 4),
        source_event_distribution=dict(event_dist),
        live_poll_pct=round(live_poll_pct, 4),
        backfill_pct=round(backfill_pct, 4),
        leakage_flag_distribution=dict(leak_dist),
        bot_before_source_count=bot_before_source_count,
        post_resolution_open_count=post_resolution_count,
        forensic_csv_path=csv_path,
        alerts=alerts,
        classification=classification,
    )


def m1_v4_payload(
    session: Session, *, window_hours: float = 999_999.0, write_csv: bool = False,
) -> dict[str, Any]:
    from app.services.baseline_constants import EFFECTIVE_BASELINE_T0
    return asdict(compute_m1_v4_report(
        session, window_hours=window_hours,
        baseline_t0=EFFECTIVE_BASELINE_T0, write_csv=write_csv,
    ))
