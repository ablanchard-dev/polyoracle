from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlmodel import Session
from sqlmodel import delete, select

from app.config import get_settings
from app.models.bot import BotLoopState, BotState
from app.models.signal import Signal
from app.models.trade import PaperTrade
from app.models.wallet import MarketFirstWalletRecord, ResolvedMarketRecord
from app.services.capital_allocator import (
    CapitalAllocator,
    PortfolioState,
    TradeSignal,
    category_fee_buffer,
)
from app.services.preset_loader import get_preset
from app.services.risk_engine import RiskDecision, RiskEngine
from app.services.risk_mode import RiskModeProfile, get_profile
from app.services.risk_mode_service import get_active_profile
from app.services.signal_cluster_engine import (
    ACTION_CONFLICTING,
    ACTION_CONSENSUS_BUILDING,
    ACTION_DUPLICATE_SAME_WALLET,
    ACTION_EXPIRED,
    ACTION_LATE_CROWD_TRAP,
    ACTION_NEW_CLUSTER,
    ACTION_PYRAMID_DENIED,
    ACTION_PYRAMID_OK,
    ACTION_TRIGGER,
    IncomingSignal,
    SignalClusterEngine,
)

logger = logging.getLogger(__name__)


# v0.7.2: single allocator preset for paper. Live runs use BASE_LIVE elsewhere.
# Capital-aware sliding scale handles selectivity per capital — no named modes.
DEFAULT_ALLOCATOR_PRESET_PAPER = "BASE_PAPER"
DEFAULT_ALLOCATOR_PRESET_LIVE = "BASE_LIVE"

# v0.7.2 — module-level SignalClusterEngine. One instance per process so
# multiple calls to ``maybe_auto_trade`` share cluster state. Tests reset
# via ``reset_cluster_engine``.
_GLOBAL_CLUSTER_ENGINE: SignalClusterEngine | None = None


def get_cluster_engine() -> SignalClusterEngine:
    global _GLOBAL_CLUSTER_ENGINE
    if _GLOBAL_CLUSTER_ENGINE is None:
        _GLOBAL_CLUSTER_ENGINE = SignalClusterEngine()
    return _GLOBAL_CLUSTER_ENGINE


def reset_cluster_engine(engine: SignalClusterEngine | None = None) -> None:
    """Reset (or replace) the module-level cluster engine. Tests use this
    to start each scenario from a clean state."""
    global _GLOBAL_CLUSTER_ENGINE
    _GLOBAL_CLUSTER_ENGINE = engine or SignalClusterEngine()


# v0.7.2 B7-C: stale paper trade sweep — pre-fix paper rows can sit in
# status='open' for days and exhaust the AGGRESSIVE max_open_positions cap.
# Mark anything older than the cutoff as 'stale_pre_fix' so it stops being
# counted by the risk engine without losing the historical record.
def sweep_stale_paper_trades(session: Session, max_age_hours: int = 24) -> int:
    """Flip papertrade.status='open' rows older than max_age_hours to
    'stale_pre_fix'. Returns how many rows were swept. Closed rows and
    rows newer than the cutoff are untouched.
    """
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    rows = list(session.exec(
        select(PaperTrade)
        .where(PaperTrade.status == "open")
        .where(PaperTrade.opened_at < cutoff)
    ))
    for row in rows:
        row.status = "stale_pre_fix"
        session.add(row)
    if rows:
        session.commit()
    return len(rows)


# v0.7.6 B9 — explicit close-reason lifecycle.
# Every paper position must reach a known terminal state. Silent deletes
# or generic "closed" are rejected by the lifecycle invariants tests.
CLOSE_REASON_RESOLVED = "CLOSED_RESOLVED"          # market settled, true outcome known
CLOSE_REASON_STALE_TTL = "CLOSED_STALE_TTL"        # > ttl_hours open, no resolution
CLOSE_REASON_MANUAL = "CLOSED_MANUAL"              # operator or kill switch
CLOSE_REASON_SIGNAL_INVALIDATED = "CLOSED_SIGNAL_INVALIDATED"  # source wallet exited

CLOSE_REASONS = (
    CLOSE_REASON_RESOLVED,
    CLOSE_REASON_STALE_TTL,
    CLOSE_REASON_MANUAL,
    CLOSE_REASON_SIGNAL_INVALIDATED,
)


def _holding_minutes(opened_at: datetime, closed_at: datetime) -> float:
    """Minutes between open and close. Handles tz-aware/naive mix safely."""
    o = opened_at.replace(tzinfo=None) if getattr(opened_at, "tzinfo", None) else opened_at
    c = closed_at.replace(tzinfo=None) if getattr(closed_at, "tzinfo", None) else closed_at
    return round((c - o).total_seconds() / 60.0, 2)


def _close_metadata_payload(
    *, holding_minutes: float, capital_released: float, exit_price: float,
    entry_fee: float = 0.0, exit_fee: float = 0.0, gross_pnl: float = 0.0,
    raw_exit_price: float | None = None, exit_slippage: float = 0.0,
    synthetic_orderbook_used: bool = False,
    resolved_category: str | None = None,
    category_source: str | None = None,
) -> str:
    """Pack close metrics into the close_reason column as a JSON suffix.

    Schema convention: ``"<REASON>|<json_metrics>"`` so simple substring
    queries on close_reason still work.

    v0.7.8 — adds B12 fee accounting fields.
    2026-05-07 — adds synthetic-orderbook tracing so we can answer
    post-paper "did the synth penalize too much?".
    2026-05-11 P0.5 — adds resolved_category + category_source so dashboards
    can show the REAL category (not "Unknown") after the category_resolver
    fallback chain (hint → Market.category → ResolvedMarketRecord → infer).
    """
    import json as _json
    payload = {
        "holding_minutes": holding_minutes,
        "capital_released_usdc": round(capital_released, 6),
        "exit_price": round(exit_price, 6),
        "entry_fee": round(entry_fee, 6),
        "exit_fee": round(exit_fee, 6),
        "gross_pnl": round(gross_pnl, 6),
        "synthetic_orderbook_used": bool(synthetic_orderbook_used),
        "exit_slippage": round(exit_slippage, 6),
    }
    if raw_exit_price is not None:
        payload["raw_exit_price"] = round(raw_exit_price, 6)
    if resolved_category is not None:
        payload["resolved_category"] = resolved_category
    if category_source is not None:
        payload["category_source"] = category_source
    return _json.dumps(payload)


def _b12_fee_for(session: Session, market_id: str | None, price: float, quantity: float) -> float:
    """Compute the Polymarket dynamic taker fee in USDC for one fill.

    Looks up the market category from the local DB and applies
    ``compute_dynamic_fee(category, price, size)``. When the market row
    is missing OR the Market table itself doesn't exist (test fixtures
    that didn't import the Market model), falls back to ``category=None``
    which yields the conservative DEFAULT_FEE_RATE — fee is still
    applied, just at the conservative default rate. This guarantees
    every paper trade pays a B12 fee in production AND in tests."""
    if not market_id or quantity <= 0 or price <= 0 or price >= 1:
        return 0.0
    from app.services.fee_engine import compute_dynamic_fee as _b12
    category: str | None = None
    try:
        from app.models.market import Market as _Market
        row = session.get(_Market, market_id)
        if row is not None:
            category = getattr(row, "category", None)
    except Exception:
        # Market table missing (test fixture) or DB error — fall through
        # to None → DEFAULT_FEE_RATE in compute_dynamic_fee.
        pass
    return float(_b12(category, price, quantity))


# 2026-05-09 — Tier auto-adapt on PnL (operator spec: "si pnl le bot change comportement").
# Reads baseline timestamp from _backups_v0_7_8_smoke_pre/T0_paper_72h.txt so we
# count only the current paper-session PnL, not the historical paper soup. Allows
# the tier to ramp NANO → TINY → MICRO etc. as the bot earns within the session.
#
# P0.2 (Round 8, 2026-05-12): when PAPER_LIVE_STRICT is on, we IGNORE the file
# baseline and use EFFECTIVE_BASELINE_T0 instead. The file T0 may pre-date the
# strict cutover, which would inflate the tier resolution by including pre-fix
# PnL. In strict mode the only legitimate baseline is the constant.
_PAPER_BASELINE_FILE = "_backups_v0_7_8_smoke_pre/T0_paper_72h.txt"


def _read_paper_baseline_at() -> datetime | None:
    try:
        import os as _os
        # File path is relative to backend/ (where dev_server.py runs).
        base_dir = _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__)))
        path = _os.path.join(base_dir, _PAPER_BASELINE_FILE)
        if not _os.path.exists(path):
            return None
        with open(path) as fh:
            ts = fh.read().strip()
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _resolve_baseline_for_capital_calc(strict_mode: bool) -> tuple[datetime | None, str]:
    """Resolve which baseline timestamp the capital calc should use.

    Returns (baseline_dt, source_tag) where source_tag is one of:
      - "effective_baseline_t0" (strict mode, always wins)
      - "file_t0" (legacy file, non-strict)
      - "none" (no baseline available — use full DB)

    In strict mode, the constant EFFECTIVE_BASELINE_T0 is the only valid
    baseline because the file may be older than the strict cutover and would
    leak pre-fix PnL into the tier resolution.
    """
    from app.services.baseline_constants import EFFECTIVE_BASELINE_T0

    # 2026-05-15 — STRICT_CUTOVER_AT override (opérateur graduated capital test).
    # Si settings.strict_cutover_at est défini ET > EFFECTIVE_BASELINE_T0, on
    # l'utilise pour reset effective capital → tier resolution recommence à
    # NANO. Permet de valider chaque tier (NANO→TINY→MICRO→SMALL...) en
    # condition réelle sans toucher le DB historique.
    try:
        _override_raw = getattr(get_settings(), "strict_cutover_at", None)
        if _override_raw:
            _override_dt = datetime.fromisoformat(str(_override_raw).replace("Z", "+00:00"))
            if _override_dt.tzinfo is None:
                from datetime import UTC as _UTC
                _override_dt = _override_dt.replace(tzinfo=_UTC)
            if _override_dt > EFFECTIVE_BASELINE_T0:
                return (_override_dt, "strict_cutover_at_override")
    except Exception:
        pass

    if strict_mode:
        return (EFFECTIVE_BASELINE_T0, "effective_baseline_t0")

    file_baseline = _read_paper_baseline_at()
    if file_baseline is None:
        return (None, "none")
    # Emit a soft warning if the file baseline pre-dates the strict cutover
    # — this would silently include obsolete PnL in non-strict mode too.
    if file_baseline < EFFECTIVE_BASELINE_T0:
        import logging
        logging.getLogger(__name__).warning(
            "compute_effective_paper_capital: file T0 (%s) pre-dates "
            "EFFECTIVE_BASELINE_T0 (%s) — non-strict mode will count pre-fix "
            "PnL. Consider enabling PAPER_LIVE_STRICT.",
            file_baseline.isoformat(),
            EFFECTIVE_BASELINE_T0.isoformat(),
        )
    return (file_baseline, "file_t0")


def compute_effective_paper_capital(session: Session, *, settings_fallback: float = 100.0) -> float:
    """Effective tier-resolution capital = starting paper_capital + session-PnL.

    The session-PnL is the cumulative realized_pnl of trades opened after the
    paper baseline timestamp. Baseline source depends on `paper_live_strict`:

      - strict mode  → EFFECTIVE_BASELINE_T0 (2026-05-11T13:21:33Z post category fix)
      - non-strict   → file `T0_paper_72h.txt` (legacy)
      - neither      → full DB (no filter)

    On a fresh DB or missing baseline file in non-strict mode, returns
    paper_capital alone.

    P0.2 (Round 8 2026-05-12): the strict-mode branch was added to prevent
    the file T0 (possibly pre-dating the strict cutover) from leaking
    pre-fix PnL into the tier resolution. Verbatim review Round 8:
    "compute_effective_paper_capital doit utiliser EFFECTIVE_BASELINE_T0
    quand PAPER_LIVE_STRICT=true. Ne plus utiliser T0_paper_72h si strict
    baseline existe."
    """
    from sqlalchemy import func as _func

    # LIVE mode: read true on-chain Polymarket collateral via CLOB API
    # (LiveWalletReader caches 60s). Paper=live invariant: sizing must use
    # the actual wallet balance, not the paper accounting.
    try:
        _settings = get_settings()
        if getattr(_settings, "live_enabled", False) is True:
            from app.services.live_wallet_reader import LiveWalletReader
            live_bal = LiveWalletReader.instance().read_usdc_balance()
            if live_bal is not None and live_bal > 0:
                return float(live_bal)
            import logging
            logging.getLogger(__name__).warning(
                "compute_effective_paper_capital: live_enabled=True but "
                "LiveWalletReader returned %r → falling back to paper logic",
                live_bal,
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "compute_effective_paper_capital: live-branch failed (%r) → paper fallback", exc,
        )

    state = session.get(BotState, 1)
    if state is None or state.paper_capital is None:
        return float(settings_fallback)
    base = float(state.paper_capital)

    # P0.2: pick baseline based on strict flag
    try:
        strict_mode = bool(getattr(get_settings(), "paper_live_strict", False))
    except Exception:
        strict_mode = False
    baseline, _src = _resolve_baseline_for_capital_calc(strict_mode)

    stmt = select(_func.sum(PaperTrade.realized_pnl))
    if baseline is not None:
        stmt = stmt.where(PaperTrade.opened_at >= baseline)
    pnl = session.exec(stmt).one()
    return base + float(pnl or 0.0)


# 2026-05-07 — Synthetic orderbook (operator decision: PnL must not be inflated).
# When _elite_paper_bypass is active the real orderbook is unavailable
# (Polymarket CLOB returns 404 on crypto 5-min markets). Without synthesis,
# entry/exit prices skip spread+slippage entirely → PnL paper >> PnL live.
# Conservative U-shape: max ~4% near 0.5 (max uncertainty, widest spread),
# floor ~1% near 0/1 (near-certain, tightest spread). Symmetric to live empirics.
_SYNTH_SPREAD_BASE = 0.01
_SYNTH_SPREAD_PEAK_EXTRA = 0.03
_SYNTH_SLIPPAGE_CAP = 0.02


def _synth_spread(price: float) -> float:
    p = max(0.0, min(1.0, float(price)))
    return _SYNTH_SPREAD_BASE + _SYNTH_SPREAD_PEAK_EXTRA * (1.0 - 2.0 * abs(p - 0.5))


def _apply_synth_exit_slippage(exit_price: float, trade_side: str) -> tuple[float, float]:
    """Returns (effective_exit_price, slippage_applied). Symmetric model:
    BUY closer = SELL at mid - spread/2. SELL closer = BUY at mid + spread/2.
    Polymarket prices are bounded to [0.0, 1.0], result clamped accordingly.
    """
    spread = _synth_spread(exit_price)
    slip = min(_SYNTH_SLIPPAGE_CAP, spread / 2.0)
    if (trade_side or "").upper() == "BUY":
        eff = max(0.0, float(exit_price) - slip)
    else:
        eff = min(1.0, float(exit_price) + slip)
    return eff, slip


def _b22_runtime_update_mfwr_on_resolved_close(
    session: Session, trade: PaperTrade,
) -> None:
    """Phase G ledger (Round 8 review audit, 2026-05-13) — single-market MFWR
    increment on RESOLVED close via dedicated dedup ledger.

    DEDUP via `WalletMarketResolutionAudit` (unique wallet+market+outcome):
      1. Insert ledger row first; UniqueConstraint raises if already counted.
      2. If insert succeeds → increment MFWR W or L, recompute wr.
      3. If insert raises IntegrityError → already counted, skip silently.
      4. audit_at is NEVER touched here (only batch B22 advances it).

    Idempotent: multiple calls for the same (wallet, market, outcome) only
    increment once. Safe to call per-PaperTrade close.
    """
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    try:
        wallet = trade.wallet_address
        market_id = trade.market_id
        if not wallet or not market_id:
            return
        from app.models.wallet import MarketFirstWalletRecord, ResolvedMarketRecord
        from app.models.trade import WalletMarketResolutionAudit
        from uuid import uuid4 as _uuid4

        # 1. Lookup RMR (with condition_id fallback — same P0.1 pattern)
        rmr = session.get(ResolvedMarketRecord, market_id)
        if rmr is None or rmr.winning_outcome_index is None:
            rmr = session.exec(
                select(ResolvedMarketRecord).where(
                    ResolvedMarketRecord.condition_id == market_id
                )
            ).first()
        if rmr is None or not rmr.closed or rmr.winning_outcome_name is None:
            return  # market not resolved in our DB — skip

        # 2. Parse RMR end_date for ledger record
        rmr_end = None
        if rmr.end_date:
            try:
                if "T" in rmr.end_date:
                    rmr_end = datetime.fromisoformat(rmr.end_date.replace("Z", "+00:00"))
                else:
                    rmr_end = datetime.fromisoformat(rmr.end_date + "+00:00")
                if rmr_end.tzinfo is None:
                    rmr_end = rmr_end.replace(tzinfo=UTC)
            except Exception:
                rmr_end = None

        # 3. Determine win vs loss
        bot_outcome = (trade.outcome or "").strip().lower()
        winner = (rmr.winning_outcome_name or "").strip().lower()
        if not bot_outcome or not winner:
            return
        is_win = (bot_outcome == winner)

        # 4. Atomic dedup: try inserting the ledger row first. The UNIQUE
        # constraint on (wallet, market, outcome) makes this idempotent.
        normalized_wallet = wallet.lower()
        ledger_row = WalletMarketResolutionAudit(
            id=str(_uuid4()),
            wallet_address=normalized_wallet,
            market_id=market_id,
            condition_id=rmr.condition_id,
            outcome=trade.outcome or "",
            resolved_winner=rmr.winning_outcome_name or "",
            resolved_at=rmr_end,
            is_win=is_win,
            source="RUNTIME_CLOSE",
            paper_trade_id=trade.id,
        )
        session.add(ledger_row)
        try:
            session.commit()
        except _IntegrityError:
            # Already counted (unique constraint) — skip increment
            session.rollback()
            return

        # 5. Ledger insert succeeded → safe to increment MFWR counters.
        mfwr = session.exec(
            select(MarketFirstWalletRecord).where(
                MarketFirstWalletRecord.address == normalized_wallet
            )
        ).first()
        if mfwr is None:
            return  # wallet not in our cohort tracking (ledger row still written for future)
        if is_win:
            mfwr.resolved_winning_markets = (mfwr.resolved_winning_markets or 0) + 1
        else:
            mfwr.resolved_losing_markets = (mfwr.resolved_losing_markets or 0) + 1
        new_w = mfwr.resolved_winning_markets or 0
        new_l = mfwr.resolved_losing_markets or 0
        if new_w + new_l > 0:
            mfwr.resolved_market_win_rate = new_w / (new_w + new_l)
        # audit_at INTENTIONALLY NOT updated — only batch B22 may advance it
        # after a full wallet scan. See _b22_incremental_update.py.
        session.add(mfwr)
        session.commit()
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        import logging
        logging.getLogger(__name__).warning(
            "B22 runtime MFWR update failed for trade %s: %s: %s",
            trade.id, type(exc).__name__, exc,
        )


def _close_paper_with_reason(
    session: Session,
    trade: PaperTrade,
    *,
    reason: str,
    exit_price: float,
) -> PaperTrade:
    """Internal helper: applies one of the v0.7.6 B9 close reasons to a
    PaperTrade and updates the metrics. Caller is responsible for the
    decision to close (TTL, resolution, manual, etc.).

    v0.7.8 — B12 fees are now subtracted from ``realized_pnl``:
      * ``entry_fee`` always charged (the BUY/SELL paid Polymarket on open)
      * ``exit_fee`` charged only when we exit BEFORE settlement
        (resolved markets settle by oracle, no exit fee in live)

    The ``trade.fees`` column is set to total fees applied. Pre-v0.7.8
    paper trades had ``realized_pnl = (exit-entry) × qty`` GROSS — paper
    PnL was 4-7% optimistic on Crypto trades. This fix closes that gap.

    Phase G (Round 8, 2026-05-13): on CLOSED_RESOLVED, additionally refresh
    the source wallet's MFWR W+L counters via _b22_runtime_update_mfwr_on_resolved_close().
    Defence-wrapped — never blocks the close.
    """
    if reason not in CLOSE_REASONS:
        raise ValueError(f"unknown close reason {reason!r}; allowed={CLOSE_REASONS}")
    # 2026-05-07: apply synthetic exit slippage on non-resolved closes.
    # RESOLVED closes settle via Polymarket oracle (no spread), so price stays raw.
    raw_exit_price = float(exit_price)
    if reason == CLOSE_REASON_RESOLVED:
        effective_exit = raw_exit_price
        exit_slippage = 0.0
        synth_used = False
    else:
        effective_exit, exit_slippage = _apply_synth_exit_slippage(exit_price, trade.side or "BUY")
        synth_used = True
    if trade.side and trade.side.upper() == "BUY":
        gross_pnl = (effective_exit - trade.average_price) * trade.quantity
    else:
        gross_pnl = (trade.average_price - effective_exit) * trade.quantity

    # v0.7.8 — B12 fee accounting
    entry_fee = _b12_fee_for(session, trade.market_id, trade.average_price, trade.quantity)
    if reason == CLOSE_REASON_RESOLVED:
        # Resolved markets settle via Polymarket oracle: winning shares
        # pay 0 fee on settle, losing shares are worthless. Only the
        # entry fee was actually charged to the trader.
        exit_fee = 0.0
    else:
        # Manual / stale-TTL / signal-invalidated = we placed an exit
        # order at effective_exit (post-slippage), paying the second leg fee.
        exit_fee = _b12_fee_for(session, trade.market_id, effective_exit, trade.quantity)
    total_fee = entry_fee + exit_fee
    trade.fees = round(total_fee, 6)
    trade.realized_pnl = round(gross_pnl - total_fee, 6)

    closed_at = datetime.utcnow()
    trade.status = "closed"
    trade.closed_at = closed_at
    holding = _holding_minutes(trade.opened_at, closed_at)
    capital_released = float(trade.notional_usd or 0.0)
    metadata = _close_metadata_payload(
        holding_minutes=holding,
        capital_released=capital_released,
        exit_price=effective_exit,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        gross_pnl=gross_pnl,
        raw_exit_price=raw_exit_price,
        exit_slippage=exit_slippage,
        synthetic_orderbook_used=synth_used,
    )
    trade.close_reason = f"{reason}|{metadata}"
    session.add(trade)

    # v0.7.8 P6 — R-based dynamic sizing state machine update (graduated).
    # The multipliers depend on the capital tier:
    #   SMALL  (<$500):    win=2.0 / loss=1.0 (growth, anti-drawdown)
    #   MEDIUM (<$5000):   win=1.5 / loss=0.75 (balanced)
    #   LARGE  (<$50000):  win=1.0 / loss=0.5 (preservation)
    #   HUGE   (≥$50000):  win=0.5 / loss=0.5 (institutional fixed)
    # See get_r_multipliers_for_capital().
    bot_state = session.get(BotState, 1)
    if bot_state is not None:
        from app.services.capital_allocator import get_r_multipliers_for_capital
        capital = float(bot_state.paper_capital or 0)
        r_win, r_loss = get_r_multipliers_for_capital(capital)
        bot_state.current_r_multiplier = r_win if trade.realized_pnl > 0 else r_loss
        session.add(bot_state)

    session.commit()
    session.refresh(trade)

    # Phase G HOTFIX (Round 8 review audit, 2026-05-13):
    # B22 runtime hook DISABLED by default because the dedup logic is unsound.
    # The helper sets `mfwr.audit_at = now()` after processing ONE market,
    # which would render invisible all OTHER un-counted historical markets
    # (rmr.end_date <= audit_at filter excludes them from the next batch).
    #
    # Re-enable only after the WalletMarketResolutionAudit ledger is in place
    # (unique(wallet, market, outcome) — see review audit verdict 2026-05-13).
    # Until then, batch B22 (_b22_incremental_update.py) remains the source
    # of truth for MFWR refreshes.
    if reason == CLOSE_REASON_RESOLVED:
        from app.config import get_settings as _gs
        try:
            if getattr(_gs(), "enable_b22_runtime_hook", False):
                _b22_runtime_update_mfwr_on_resolved_close(session, trade)
        except Exception:
            pass

    return trade


def auto_close_stale_papertrades(
    session: Session, *, ttl_hours: int = 24, mark_price_lookup=None
) -> dict:
    """v0.7.6 B9 — close all status='open' papertrades older than ttl_hours
    with reason CLOSED_STALE_TTL.

    For each candidate:
    - exit_price = current market mark if ``mark_price_lookup`` is provided
      and returns a value, else the trade's entry price (no PnL).
    - realized_pnl = (exit - entry) * size (signed by side)
    - capital_released = notional_usd
    - holding_minutes = closed_at - opened_at

    Returns ``{"closed": N, "trade_ids": [...]}``. Never deletes. Closed rows
    keep their PnL history per spec ('no_silent_delete').
    """
    cutoff = datetime.utcnow() - timedelta(hours=ttl_hours)
    rows = list(session.exec(
        select(PaperTrade)
        .where(PaperTrade.status == "open")
        .where(PaperTrade.opened_at < cutoff)
    ))
    closed_ids: list[str] = []
    for row in rows:
        # Default exit price: use entry (zero realized PnL) when no live mark.
        exit_price = row.average_price
        if mark_price_lookup is not None:
            try:
                mark = mark_price_lookup(row.market_id, row.outcome, row.side)
                if mark is not None and mark > 0:
                    exit_price = float(mark)
            except Exception:  # noqa: BLE001
                pass
        _close_paper_with_reason(
            session, row, reason=CLOSE_REASON_STALE_TTL, exit_price=exit_price,
        )
        closed_ids.append(row.id)
    return {"closed": len(closed_ids), "trade_ids": closed_ids}


def auto_close_resolved_papertrades(session: Session) -> dict:
    """v0.7.6 B9 — close all status='open' papertrades whose Market is now
    resolved (winning outcome known). Realized PnL computed from the
    resolved outcome (winner pays 1.0, loser 0.0).
    """
    from app.models.market import Market
    rows = list(session.exec(
        select(PaperTrade).where(PaperTrade.status == "open")
    ))
    closed_ids: list[str] = []
    for row in rows:
        market = session.get(Market, row.market_id) if row.market_id else None
        if market is None:
            continue
        # Heuristic: market is "resolved" if it has yes_price/no_price set
        # at 0.0 or 1.0 boundary — Polymarket binary markets settle to those.
        yes = getattr(market, "yes_price", None)
        no = getattr(market, "no_price", None)
        winner = None
        if yes is not None and yes >= 0.99:
            winner = "YES"
        elif no is not None and no >= 0.99:
            winner = "NO"
        if winner is None:
            continue
        # Settle: winning outcome -> 1.0, losing -> 0.0
        outcome_upper = (row.outcome or "").upper()
        side_upper = (row.side or "BUY").upper()
        if outcome_upper == winner:
            settle_price = 1.0
        else:
            settle_price = 0.0
        # For SELL, the convention is reversed in close_paper_with_reason
        _close_paper_with_reason(
            session, row,
            reason=CLOSE_REASON_RESOLVED,
            exit_price=settle_price,
        )
        closed_ids.append(row.id)
    return {"closed": len(closed_ids), "trade_ids": closed_ids}


def manual_close_papertrade(session: Session, trade_id: str, exit_price: float) -> PaperTrade:
    """v0.7.6 B9 — operator-driven close. Always writes CLOSED_MANUAL."""
    trade = session.get(PaperTrade, trade_id)
    if trade is None:
        raise ValueError(f"papertrade not found: {trade_id}")
    if trade.status != "open":
        raise ValueError(f"papertrade {trade_id} is not open (status={trade.status})")
    return _close_paper_with_reason(
        session, trade, reason=CLOSE_REASON_MANUAL, exit_price=float(exit_price),
    )


def close_signal_invalidated(
    session: Session, *, wallet_address: str, market_id: str, exit_price: float,
) -> dict:
    """v0.7.6 B9 — when the source wallet exits a position we copied,
    close OUR open papertrade(s) on the same market with reason
    CLOSED_SIGNAL_INVALIDATED."""
    rows = list(session.exec(
        select(PaperTrade)
        .where(PaperTrade.status == "open")
        .where(PaperTrade.wallet_address == wallet_address)
        .where(PaperTrade.market_id == market_id)
    ))
    closed_ids: list[str] = []
    for row in rows:
        _close_paper_with_reason(
            session, row,
            reason=CLOSE_REASON_SIGNAL_INVALIDATED,
            exit_price=float(exit_price),
        )
        closed_ids.append(row.id)
    return {"closed": len(closed_ids), "trade_ids": closed_ids}


# v0.7.7 B9.1 — periodic background close-loop.
#
# v0.7.6 confirmed the cap saturates ~30 min into a long run because
# auto_close_* only fire on startup. This function bundles the two
# server-callable closers (resolved + stale TTL) into a single safe
# periodic invocation. Caller (polling loop or scheduled task) decides
# the cadence — recommended every BACKGROUND_CLOSE_LOOP_INTERVAL_S.

import os as _os

BACKGROUND_CLOSE_LOOP_INTERVAL_S = int(
    _os.environ.get("BACKGROUND_CLOSE_LOOP_INTERVAL_S", "600")
)


def run_background_close_loop_iteration(
    session: Session, *, ttl_hours: int = 24, mark_price_lookup=None,
) -> dict:
    """Single iteration of the background close loop.

    1. Calls ``auto_close_resolved_papertrades`` first — markets that
       settled get realized PnL based on the winning outcome.
    2. Then ``auto_close_stale_papertrades`` for anything that's been
       open longer than ``ttl_hours`` (default 24).

    Returns a summary dict with counts per reason. The caller logs and
    schedules the next iteration. NEVER deletes a row.
    """
    resolved = auto_close_resolved_papertrades(session)
    stale = auto_close_stale_papertrades(
        session, ttl_hours=ttl_hours, mark_price_lookup=mark_price_lookup,
    )
    return {
        "closed_resolved": resolved["closed"],
        "closed_stale_ttl": stale["closed"],
        "total_closed": resolved["closed"] + stale["closed"],
        "trade_ids": resolved["trade_ids"] + stale["trade_ids"],
    }


# Map cluster action codes → NoTradeDecisionLog reason_codes.
CLUSTER_BLOCK_REASONS: dict[str, str] = {
    ACTION_DUPLICATE_SAME_WALLET: "CLUSTER_DUPLICATE",
    ACTION_CONFLICTING: "CLUSTER_CONFLICT",
    ACTION_LATE_CROWD_TRAP: "CLUSTER_LATE_CROWD",
    ACTION_PYRAMID_DENIED: "CLUSTER_ALREADY_TRADED",
    ACTION_EXPIRED: "CLUSTER_EXPIRED",
    ACTION_CONSENSUS_BUILDING: "CLUSTER_PENDING_CONSENSUS",
    ACTION_NEW_CLUSTER: "CLUSTER_PENDING_CONSENSUS",
}


class PaperTradingEngine:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        # v0.7.8 P5-A — capital source-of-truth is BotState (DB), not
        # Settings (config default). Reading from DB ensures the engine
        # uses the same value the operator sees in /bot/status. Without
        # this, Settings default (1000) was used despite BotState=100,
        # causing STRONG-pass-through at the 100€ smoke (tier resolved
        # to STANDARD instead of ULTRA_SELECTIVE).
        self.paper_capital = self._effective_paper_capital()

    def _effective_paper_capital(self) -> float:
        """Effective capital = paper_capital + session-PnL (cumulative realized
        PnL since T0_paper_72h baseline). Drives tier ramping NANO→TINY→MICRO."""
        return compute_effective_paper_capital(self.session, settings_fallback=self.settings.paper_capital)

    def _decision_capital_snapshot(self, effective_override: float | None = None) -> dict[str, Any]:
        """P0-A fix bug #70 — fresh capital snapshot for trade decision.

        Returns ALL derived capital fields recomputed AT THE MOMENT of the call,
        used by maybe_auto_trade to ensure exposure / cap / tier resolution
        ALL see the same fresh effective_capital. Prevents the cached
        `self.paper_capital` from going stale during long-lived engine.

        ARGS
        ====
        effective_override : if provided, use this value instead of re-reading
            _effective_paper_capital(). Used by callers (e.g. maybe_auto_trade)
            that want **a single capital read per decision** for full coherence.

        Includes:
          - paper_base : BotState.paper_capital (operator baseline)
          - effective_capital : base + cumul PnL post-T0 (or live wallet if live)
          - open_notional_usd : sum of current open positions
          - capital_available : effective - open_notional
          - tier_name + max_positions + max_total_exposure : from CAPITAL_TIER_RULES
          - max_total_exposure_usd : effective × tier.max_total_exposure

        Used for both decision-time refresh AND no-trade decision logging.
        """
        from app.services.capital_allocator import _resolve_tier
        from app.models.bot import BotState as _BotState

        effective = effective_override if effective_override is not None else self._effective_paper_capital()
        state = self.session.get(_BotState, 1)
        paper_base = float(state.paper_capital) if state and state.paper_capital else float(self.settings.paper_capital)
        open_notional = self.open_notional_usd()
        tier = _resolve_tier(effective)
        max_total_exposure_pct = float(tier.get("max_total_exposure_cap", 0.95))
        return {
            "paper_base": paper_base,
            "effective_capital": effective,
            "open_notional_usd": open_notional,
            "capital_available": max(0.0, round(effective - open_notional, 4)),
            "tier_name": tier.get("name"),
            "max_positions_cap": int(tier.get("max_open_positions_cap", 0)),
            "max_total_exposure_pct": max_total_exposure_pct,
            "max_total_exposure_usd": round(effective * max_total_exposure_pct, 2),
        }

    # ---------------- existing helpers ----------------

    def open_paper_position(
        self,
        market_id: str,
        outcome: str,
        side: str,
        quantity: float,
        price: float,
        spread: float,
        signal_id: str | None = None,
        wallet_address: str | None = None,
        auto: bool = False,
        notional_usd: float | None = None,
        allocator_checked: bool = False,
        entry_audit: dict | None = None,
    ) -> PaperTrade:
        """Open a paper position and (P0.4) persist its entry-price provenance.

        Args:
            entry_audit: optional dict with the provenance fields captured at
                open time (passed by `_run_open_with_audit()`). Keys:
                - raw_signal_price: source wallet's trade price
                - gamma_mid_at_open: live Gamma price re-fetched
                - clob_best_bid_at_open / clob_best_ask_at_open
                - spread_at_open / liquidity_score_at_open
                - entry_price_source: GAMMA / CLOB / SYNTH / WALLET_FROZEN_FALLBACK
                - price_source_confidence: HIGH / MEDIUM / LOW
                - source_trade_id, source_wallet
                If None, no EntryPriceAudit row is created (legacy callers).
        """
        if auto and not allocator_checked:
            raise PermissionError("Auto paper trades must pass CapitalAllocator before opening")
        # 2026-05-07: when bypass active, incoming spread = 0 (no orderbook).
        # Synthesize a conservative spread from yes_price so entry is realistic.
        # Polymarket prices in [0,1] — clamp both sides.
        effective_spread = spread if spread > 0.001 else _synth_spread(price)
        slippage = min(_SYNTH_SLIPPAGE_CAP, effective_spread / 2.0)
        if side.upper() == "BUY":
            entry_price = min(1.0, price + slippage)
        else:
            entry_price = max(0.0, price - slippage)
        notional = notional_usd if notional_usd is not None else round(entry_price * quantity, 2)
        trade = PaperTrade(
            id=str(uuid4()),
            market_id=market_id,
            outcome=outcome,
            side=side,
            quantity=quantity,
            average_price=entry_price,
            slippage=slippage,
            signal_id=signal_id,
            wallet_address=wallet_address,
            notional_usd=notional,
            auto=auto,
        )
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        # P0.4: persist entry price provenance for paper-vs-live audit + M1 v5
        if entry_audit is not None:
            self._record_entry_price_audit(trade, entry_price, effective_spread, entry_audit)

        return trade

    def _record_entry_price_audit(
        self,
        trade: PaperTrade,
        chosen_entry_price: float,
        effective_spread: float,
        audit_data: dict,
    ) -> None:
        """P0.4 — create an EntryPriceAudit row for paper-vs-live calibration.

        Failure to write the audit must NEVER block the trade open (the trade
        is already committed). On error we log and continue.
        """
        try:
            from app.models.trade import EntryPriceAudit

            gamma_mid = audit_data.get("gamma_mid_at_open")
            clob_bid = audit_data.get("clob_best_bid_at_open")
            clob_ask = audit_data.get("clob_best_ask_at_open")

            # Live shadow entry price: best_ask for BUY, best_bid for SELL.
            # Fallback: gamma_mid ± synth_spread/2 (matches what live would pay
            # approximately when CLOB is unavailable).
            side_up = (trade.side or "BUY").upper()
            shadow = None
            if side_up == "BUY":
                if clob_ask is not None:
                    shadow = clob_ask
                elif gamma_mid is not None:
                    shadow = min(1.0, gamma_mid + effective_spread / 2.0)
            else:  # SELL
                if clob_bid is not None:
                    shadow = clob_bid
                elif gamma_mid is not None:
                    shadow = max(0.0, gamma_mid - effective_spread / 2.0)

            slip_abs = (
                abs(shadow - chosen_entry_price) if shadow is not None and chosen_entry_price else None
            )
            slip_pct = (
                slip_abs / chosen_entry_price if slip_abs is not None and chosen_entry_price else None
            )

            row = EntryPriceAudit(
                id=str(uuid4()),
                paper_trade_id=trade.id,
                market_id=trade.market_id,
                outcome=trade.outcome,
                side=side_up,
                source_trade_id=audit_data.get("source_trade_id"),
                source_wallet=audit_data.get("source_wallet") or trade.wallet_address,
                raw_signal_price=audit_data.get("raw_signal_price"),
                gamma_mid_at_open=gamma_mid,
                clob_best_bid_at_open=clob_bid,
                clob_best_ask_at_open=clob_ask,
                spread_at_open=audit_data.get("spread_at_open", effective_spread),
                liquidity_score_at_open=audit_data.get("liquidity_score_at_open"),
                chosen_entry_price=chosen_entry_price,
                entry_price_source=audit_data.get("entry_price_source", "UNKNOWN"),
                price_source_confidence=audit_data.get("price_source_confidence", "LOW"),
                live_shadow_entry_price=shadow,
                live_shadow_slippage_abs=slip_abs,
                live_shadow_slippage_pct=slip_pct,
                opened_at=trade.opened_at,
            )
            self.session.add(row)
            self.session.commit()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to persist EntryPriceAudit for trade %s: %s", trade.id, exc,
            )

    def close_paper_position(self, trade: PaperTrade, exit_price: float, reason: str | None = None) -> PaperTrade:
        # 2026-05-07: same synthetic exit slippage as _close_paper_with_reason.
        # 2026-05-11 (P0.4 hot-fix): treat "market_resolved" as resolved (the
        # string used by evaluate_open_positions). Pre-fix, only the canonical
        # constant CLOSE_REASON_RESOLVED matched, causing slippage+exit_fee
        # applied wrongly to oracle settlements (paper PnL underestimated).
        _is_resolved_close = reason in (CLOSE_REASON_RESOLVED, "market_resolved")
        if _is_resolved_close:
            effective_exit = float(exit_price)
        else:
            effective_exit, _ = _apply_synth_exit_slippage(exit_price, trade.side or "BUY")
        if trade.side.upper() == "BUY":
            gross_pnl = (effective_exit - trade.average_price) * trade.quantity
        else:
            gross_pnl = (trade.average_price - effective_exit) * trade.quantity
        # v0.7.8 — B12 fee accounting on the legacy close path. Same
        # semantics as _close_paper_with_reason: entry fee always; exit
        # fee unless this is an explicit RESOLVED close (manual closes
        # are mid-market exits = both legs charged).
        entry_fee = _b12_fee_for(self.session, trade.market_id, trade.average_price, trade.quantity)
        if _is_resolved_close:
            exit_fee = 0.0
        else:
            exit_fee = _b12_fee_for(self.session, trade.market_id, effective_exit, trade.quantity)
        total_fee = entry_fee + exit_fee
        trade.fees = round(total_fee, 6)
        trade.realized_pnl = round(gross_pnl - total_fee, 2)
        trade.status = "closed"
        trade.closed_at = datetime.now(UTC)
        if reason is not None:
            trade.close_reason = reason
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)
        return trade

    def update_paper_positions(self) -> list[PaperTrade]:
        return list(self.session.exec(select(PaperTrade).where(PaperTrade.status == "open")).all())

    def compute_paper_pnl(self) -> float:
        # 2026-05-07 — leak fix: SQL SUM instead of loading all rows.
        # 2026-05-11 (A-T0 amendment): when BotState.strict_cutover_at is set,
        # only sum trades opened AFTER cutover. Pre-cutover trades were taken
        # under the legacy ELITE-paper bypass and are NOT representative of
        # strict-mode PnL (operator decision: zero capital preservation when
        # bypass is killed — baseline must measure pure strict mode).
        from sqlalchemy import func
        state = self.session.get(BotState, 1)
        cutover = getattr(state, "strict_cutover_at", None) if state else None
        if cutover is not None:
            if cutover.tzinfo is None:
                cutover = cutover.replace(tzinfo=UTC)
            total = self.session.exec(
                select(func.sum(PaperTrade.realized_pnl))
                .where(PaperTrade.opened_at >= cutover)
            ).one()
        else:
            total = self.session.exec(select(func.sum(PaperTrade.realized_pnl))).one()
        return round(float(total or 0.0), 2)

    def compute_unrealized_pnl(self) -> float:
        return 0.0

    def compute_realized_pnl(self) -> float:
        return self.compute_paper_pnl()

    def apply_spread(self, price: float, spread: float, side: str = "buy") -> float:
        return price + spread / 2 if side == "buy" else price - spread / 2

    def estimate_slippage(self, quantity: float, liquidity: float) -> float:
        if liquidity <= 0:
            return 0.05
        return min(0.05, quantity / liquidity * 0.02)

    def simulate_partial_fill(self, quantity: float, liquidity: float) -> float:
        return min(quantity, liquidity * 0.05)

    def export_trade_journal(self) -> list[dict]:
        return [trade.model_dump() for trade in self.session.exec(select(PaperTrade)).all()]

    def compare_strategy_vs_baseline(self) -> dict[str, float]:
        return {"filtered_copy": 0.0, "naive_copy": 0.0}

    def generate_paper_report(self) -> dict[str, float | int]:
        # 2026-05-07 — leak fix: SQL aggregates instead of loading all rows.
        from sqlalchemy import func
        open_count = self.session.exec(
            select(func.count(PaperTrade.id)).where(PaperTrade.status == "open")
        ).one() or 0
        closed_count = self.session.exec(
            select(func.count(PaperTrade.id)).where(PaperTrade.status == "closed")
        ).one() or 0
        wins_count = self.session.exec(
            select(func.count(PaperTrade.id))
            .where(PaperTrade.status == "closed")
            .where(PaperTrade.realized_pnl > 0)
        ).one() or 0
        return {
            "open_positions": int(open_count),
            "closed_trades": int(closed_count),
            "realized_pnl": self.compute_realized_pnl(),
            "win_rate": round(int(wins_count) / int(closed_count), 3) if closed_count else 0.0,
        }

    def reset(self) -> None:
        self.session.exec(delete(PaperTrade))
        self.session.commit()

    # ---------------- v0.4 auto trading ----------------

    def _bot_state(self) -> BotState | None:
        try:
            return self.session.get(BotState, 1)
        except Exception:
            return None

    def _bot_loop_state(self) -> BotLoopState | None:
        try:
            return self.session.get(BotLoopState, 1)
        except Exception:
            return None

    def _kill_switch_active(self) -> bool:
        loop_state = self._bot_loop_state()
        if loop_state and loop_state.kill_switch_active:
            return True
        bot_state = self._bot_state()
        return bool(bot_state and bot_state.kill_switch_active)

    def _bot_mode(self) -> str:
        loop_state = self._bot_loop_state()
        if loop_state and loop_state.mode:
            return loop_state.mode
        bot_state = self._bot_state()
        if bot_state and bot_state.mode:
            return bot_state.mode
        return self.settings.default_bot_mode

    def current_exposure(self) -> float:
        positions = self.update_paper_positions()
        capital = max(self.paper_capital, 1.0)
        notional = sum(trade.notional_usd or (trade.average_price * trade.quantity) for trade in positions)
        return round(notional / capital, 4)

    def open_notional_usd(self) -> float:
        return round(
            sum(
                trade.notional_usd or (trade.average_price * trade.quantity)
                for trade in self.update_paper_positions()
            ),
            4,
        )

    def market_exposure(self, market_id: str) -> float:
        positions = [trade for trade in self.update_paper_positions() if trade.market_id == market_id]
        capital = max(self.paper_capital, 1.0)
        notional = sum(trade.notional_usd or (trade.average_price * trade.quantity) for trade in positions)
        return round(notional / capital, 4)

    def market_exposure_usd(self, market_id: str) -> float:
        positions = [trade for trade in self.update_paper_positions() if trade.market_id == market_id]
        return round(
            sum(trade.notional_usd or (trade.average_price * trade.quantity) for trade in positions),
            4,
        )

    def daily_pnl(self) -> float:
        # SQLite stores datetimes offset-naive; align cutoff to avoid TypeError.
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
        trades = self.session.exec(select(PaperTrade).where(PaperTrade.status == "closed")).all()
        return round(sum(trade.realized_pnl for trade in trades if trade.closed_at and trade.closed_at >= cutoff), 2)

    def weekly_pnl(self) -> float:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)
        trades = self.session.exec(select(PaperTrade).where(PaperTrade.status == "closed")).all()
        return round(sum(trade.realized_pnl for trade in trades if trade.closed_at and trade.closed_at >= cutoff), 2)

    def has_open_position_for_signal(self, signal_id: str) -> bool:
        if not signal_id:
            return False
        return any(trade.signal_id == signal_id for trade in self.update_paper_positions())

    def has_open_position_for_market_outcome(self, market_id: str, outcome: str | None, wallet_address: str | None = None) -> bool:
        for trade in self.update_paper_positions():
            if trade.market_id != market_id:
                continue
            if outcome and trade.outcome != outcome:
                continue
            if wallet_address and trade.wallet_address and trade.wallet_address != wallet_address:
                continue
            return True
        return False

    def wallet_exposure(self, wallet_address: str | None) -> float:
        if not wallet_address:
            return 0.0
        capital = max(self.paper_capital, 1.0)
        notional = sum(
            trade.notional_usd or (trade.average_price * trade.quantity)
            for trade in self.update_paper_positions()
            if (trade.wallet_address or "").lower() == wallet_address.lower()
        )
        return round(notional / capital, 4)

    def wallet_exposure_usd(self, wallet_address: str | None) -> float:
        if not wallet_address:
            return 0.0
        return round(
            sum(
                trade.notional_usd or (trade.average_price * trade.quantity)
                for trade in self.update_paper_positions()
                if (trade.wallet_address or "").lower() == wallet_address.lower()
            ),
            4,
        )

    def daily_trade_count(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=1)
        count = 0
        for trade in self.session.exec(select(PaperTrade)).all():
            opened = trade.opened_at
            if opened is None:
                continue
            # SQLite drops the tzinfo on round-trip; reattach UTC so the
            # comparison against the timezone-aware cutoff doesn't blow up.
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            if opened >= cutoff:
                count += 1
        return count

    def lookup_candidate_status(self, address: str | None) -> str | None:
        if not address:
            return None
        record = self.session.get(MarketFirstWalletRecord, address.lower())
        return record.candidate_status if record else None

    def lookup_wallet_record(self, address: str | None) -> MarketFirstWalletRecord | None:
        if not address:
            return None
        return self.session.get(MarketFirstWalletRecord, address.lower())

    # v0.7.2 B4 fix — wallet_tier authority.
    #
    # ``audit.wallet_tier`` is computed at audit time by the legacy
    # smart_wallet_auditor and tends to disagree with the v0.7.0 truth
    # pool (~65% of audits in the 4h smoke window were tagged ``WATCH``
    # despite the wallet being ELITE/STRONG in MFWR). The TRUTH is
    # ``MarketFirstWalletRecord.candidate_status`` because the v0.7.0 sync
    # rebuilds it from the validated paper universe each cycle.
    #
    # Priority order: MFWR > audit_context override > legacy audit tag.
    @staticmethod
    def resolve_effective_tier(
        audit_context: dict[str, Any],
        wallet_record: MarketFirstWalletRecord | None,
    ) -> str:
        """Return the effective wallet tier string (uppercase).

        Priority:
          1. ``wallet_record.candidate_status`` — truth pool (v0.7.0 sync)
          2. ``audit_context['candidate_status']`` — explicit override
          3. ``audit_context['wallet_tier']``     — legacy audit tag
          4. fallback ``'UNKNOWN'``
        """
        if wallet_record is not None:
            cstatus = (wallet_record.candidate_status or "").strip()
            if cstatus:
                return cstatus.upper()
        ctx_status = (audit_context.get("candidate_status") or "").strip()
        if ctx_status:
            return ctx_status.upper()
        ctx_tier = (audit_context.get("wallet_tier") or "").strip()
        if ctx_tier:
            return ctx_tier.upper()
        return "UNKNOWN"

    def _allocator_preset_name(self, profile: RiskModeProfile) -> str:
        # v0.7.2: legacy `profile` argument kept for signature stability but
        # ignored — the bot has ONE operational preset (paper). Capital-aware
        # tightening inside CapitalAllocator does the per-capital adaptation.
        return DEFAULT_ALLOCATOR_PRESET_PAPER

    def _fetch_execution_price_now(
        self,
        market_id: str | None,
        outcome: str | None,
        side: str | None,
    ) -> tuple[float | None, str]:
        """v0.7.8 — re-fetch the CURRENT market price for the outcome we
        are about to copy, instead of using the wallet's frozen
        traded_at price.

        The wallet's price is the price at which the source wallet
        executed; by the time our 90s polling sees the trade and the
        allocator approves a paper open, the market price has typically
        moved (especially on ULTRA_SHORT 1MN/2MN markets where prices
        can swing 10-30% in seconds). Using the wallet's price as the
        paper entry is structurally optimistic — paper PnL > real PnL
        on fast-moving markets. This fix re-fetches the live Gamma
        outcomePrices at the moment we'd open the position, picks the
        price for the matching outcome, and returns it.

        Returns ``(price, source)`` where ``source`` is one of:
            ``"gamma_refetched"`` — fresh price from Gamma at open time
            ``"wallet_frozen_fallback"`` — gamma fetch failed, caller
                                          should keep using wallet price
            ``"market_resolved"`` — market is settled (yes/no at boundary),
                                    skip the trade entirely

        Returns ``(None, ...)`` on outright failure so the caller can
        decide whether to fall back to the wallet price or skip.
        """
        if not market_id or not outcome:
            return None, "wallet_frozen_fallback"
        try:
            from app.services.polymarket.gamma_client import GammaClient
            details = GammaClient().fetch_market_by_condition(market_id)
        except Exception:
            return None, "wallet_frozen_fallback"

        if not details:
            return None, "wallet_frozen_fallback"

        # Resolved-market guard. Gamma marks ``closed`` and outcomePrices
        # snap to {0, 1}. Refusing to open a position on a settled market
        # closes a real exposure: at small capital we'd be locking
        # capital on a trade that cannot move.
        if details.get("closed") is True:
            return None, "market_resolved"

        # Outcome → price mapping. Polymarket Gamma returns ``outcomes``
        # as a JSON-string list and ``outcomePrices`` as a JSON-string
        # list aligned by index.
        import json as _json
        try:
            outcomes_raw = details.get("outcomes")
            prices_raw = details.get("outcomePrices")
            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        except Exception:
            return None, "wallet_frozen_fallback"
        if not isinstance(outcomes, list) or not isinstance(prices, list):
            return None, "wallet_frozen_fallback"
        if len(outcomes) != len(prices):
            return None, "wallet_frozen_fallback"

        # Boundary guard — if any outcome price is at 0.99/0.01 the
        # market is effectively resolved even without ``closed=true``.
        try:
            float_prices = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None, "wallet_frozen_fallback"
        if any(p >= 0.99 for p in float_prices) or all(p <= 0.01 for p in float_prices):
            return None, "market_resolved"

        # Find our outcome (case-insensitive match).
        target = (outcome or "").strip().lower()
        for idx, name in enumerate(outcomes):
            if str(name).strip().lower() == target:
                return float_prices[idx], "gamma_refetched"

        # Outcome name didn't match — log nothing here, caller decides.
        return None, "wallet_frozen_fallback"

    def _lookup_expected_holding_hours(self, market_id: str | None) -> float | None:
        """v0.7.8 P3 — fetch the market's expected resolution time so the
        allocator can apply its UNKNOWN_DEADLINE and B13 duration gates.

        Returns ``None`` when the market is not in DB (= UNKNOWN_DEADLINE
        rejection at small capital). Returns ``0.0001`` for already-resolved
        markets so the duration filter sees a valid (very short) window.
        """
        if not market_id:
            return None
        try:
            from app.models.market import Market
            market = self.session.get(Market, market_id)
            if market is None:
                return None
            exp_min = getattr(market, "expected_resolution_minutes", None)
            if exp_min is None:
                # Market exists but no resolution data — treat as None
                # so UNKNOWN_DEADLINE fires at small capital.
                return None
            # Floor at 0.0001 hour (= 0.36s) to keep the value truthy
            # so allocator distinguishes "known but short" from "unknown".
            return max(0.0001, float(exp_min) / 60.0)
        except Exception:
            return None

    @staticmethod
    def _normalise_edge(value: Any) -> float:
        try:
            score = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return round(score / 100.0, 6) if score > 1.0 else score

    def _capital_available_usd(self) -> float:
        return max(0.0, round(self.paper_capital - self.open_notional_usd(), 4))

    def _log_visibility_leakage(
        self, *, signal: Signal, audit_context: dict, source_trade_id: str,
        source_seen_at: datetime, now_utc: datetime, delta_s: float,
    ) -> None:
        """P0.3 M1 v5 — log a NoTradeDecision when the visibility gate refuses
        a causally-impossible trade (source.seen_at > now + 2s tolerance)."""
        try:
            from app.services.risk_engine import RiskEngine
            details = {
                "gate": "M1_V5_VISIBILITY",
                "source_trade_id": source_trade_id,
                "source_seen_at": source_seen_at.isoformat(),
                "bot_now": now_utc.isoformat(),
                "delta_seconds_after_now": round(delta_s, 3),
                "explanation": (
                    "Source PublicTrade.seen_at is later than bot's open moment. "
                    "Live cannot reproduce this — likely backfill, wrong join, or "
                    "clock skew. Trade refused."
                ),
                "wallet": audit_context.get("wallet"),
            }
            decision = RiskDecision(
                approved=False,
                reason="M1 v5 visibility gate: source not visible yet at open time",
                reason_code="VISIBILITY_LEAKAGE_SUSPECT",
                details=details,
            )
            RiskEngine().log_no_trade(
                self.session,
                decision,
                signal_id=signal.id,
                market_id=signal.market_id,
                wallet_address=audit_context.get("wallet"),
                details=details,
            )
        except Exception as exc:
            # Never crash on logging — but log the swallowed exception for debug
            import logging
            logging.getLogger(__name__).warning(
                "M1 v5 visibility log failed: %s: %s", type(exc).__name__, exc,
            )

    def _log_allocator_no_trade(
        self,
        risk: RiskEngine,
        allocator_decision: Any,
        signal: Signal,
        wallet_address: str | None,
        audit_details: dict[str, Any],
    ) -> None:
        decision = RiskDecision(
            approved=False,
            reason=f"Allocator rejected trade: {allocator_decision.reason_code}",
            reason_code=allocator_decision.reason_code or "INSUFFICIENT_DATA",
            details=audit_details,
        )
        risk.log_no_trade(
            self.session,
            decision,
            signal_id=signal.id,
            market_id=signal.market_id,
            wallet_address=wallet_address,
            details=audit_details,
        )
        self._maybe_log_shadow_bypass(
            reason_code=allocator_decision.reason_code,
            signal=signal,
            wallet_address=wallet_address,
            candidate_status=str(audit_details.get("status") or audit_details.get("tier") or ""),
            audit_details=audit_details,
        )

    # ------- A4 (2026-05-11): Shadow log inversé "would_have_passed_bypass" -------
    # When PAPER_LIVE_STRICT is on and we reject a trade for a reason the
    # legacy _elite_paper_bypass would have skipped, emit an additional
    # NoTradeDecision with reason_code=SHADOW_BYPASS_<original>. Lets the
    # operator quantify the "fake cadence" of the pre-strict era via a
    # simple SQL: SELECT reason_code, COUNT(*) FROM notradedecision
    # WHERE reason_code LIKE 'SHADOW_BYPASS_%' GROUP BY reason_code.
    _SHADOW_BYPASSABLE_CODES = frozenset({
        "WIDE_SPREAD",
        "LOW_LIQUIDITY",
        "BAD_ORDERBOOK",
        "INSUFFICIENT_DATA",
        "NO_COPYABLE_EDGE",
        "UNKNOWN_CATEGORY",
        "UNKNOWN_DEADLINE",
    })

    def _maybe_log_shadow_bypass(
        self,
        *,
        reason_code: str | None,
        signal: Signal,
        wallet_address: str | None,
        candidate_status: str,
        audit_details: dict[str, Any] | None = None,
    ) -> None:
        if not getattr(self.settings, "paper_live_strict", False):
            return
        if not self.settings.paper_trading_enabled:
            return
        if candidate_status != "ELITE":
            return
        if not reason_code or reason_code not in self._SHADOW_BYPASSABLE_CODES:
            return
        from uuid import uuid4 as _uuid4
        import json as _json
        from app.models.trade import NoTradeDecision as _NoTradeDecision
        shadow = _NoTradeDecision(
            id=str(_uuid4()),
            signal_id=signal.id,
            market_id=signal.market_id,
            wallet_address=wallet_address,
            reason_code=f"SHADOW_BYPASS_{reason_code}",
            details=_json.dumps({
                "original_reason_code": reason_code,
                "would_have_passed_under_bypass": True,
                "wallet_tier": candidate_status,
                "audit_details": audit_details or {},
            }),
        )
        self.session.add(shadow)
        self.session.commit()

    def _cluster_check(
        self,
        signal: Signal,
        audit_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Run the SignalClusterEngine over the incoming signal.

        Returns:
          * ``None`` when the cluster says we should proceed (TRIGGER or
            PYRAMID_OK), OR when the caller's wallet tier is non-tradable
            so the allocator should emit the proper ``WALLET_TIER`` reject
            (skipping the cluster avoids polluting clusters with junk
            signals from blocked / unknown tiers).
          * A blocking dict (``executed=False`` + reason_code +
            cluster details) when the engine says no.
        """
        wallet_address = (audit_context.get("wallet") or "").lower()
        wallet_record = self.lookup_wallet_record(wallet_address) if wallet_address else None
        wallet_tier = self.resolve_effective_tier(audit_context, wallet_record)
        # Don't pollute clusters with non-tradable tiers — let the allocator
        # emit WALLET_TIER directly.
        if wallet_tier not in {"ELITE", "STRONG", "CANDIDATE_ELITE"}:
            return None
        side = str(audit_context.get("side") or "BUY").upper()
        midpoint = audit_context.get("current_midpoint") or audit_context.get("price") or 0.0
        try:
            entry_price_f = float(audit_context.get("price") or 0.0)
        except (TypeError, ValueError):
            entry_price_f = 0.0
        try:
            midpoint_f = float(midpoint or 0.0)
        except (TypeError, ValueError):
            midpoint_f = entry_price_f
        try:
            notional_f = float(signal.proposed_size_usd or 0.0)
        except (TypeError, ValueError):
            notional_f = 0.0

        incoming = IncomingSignal(
            wallet_address=wallet_address,
            wallet_tier=wallet_tier,
            market_id=signal.market_id or "",
            outcome=str(signal.outcome or ""),
            side=side,
            entry_price=entry_price_f,
            notional_usd=notional_f,
            current_midpoint=midpoint_f,
        )
        cluster_event = get_cluster_engine().add_signal(incoming)

        # Pyramid_OK and Trigger flow through to allocator
        if cluster_event.action in (ACTION_TRIGGER, ACTION_PYRAMID_OK):
            audit_context["_cluster_event"] = cluster_event
            return None

        # v0.7.8 P3 (C encadré) — single STRONG strict policy.
        # When cluster engine returns NEW_CLUSTER / CONSENSUS_BUILDING
        # for a single STRONG (consensus_score = 0.5 < threshold 1.0),
        # apply the strict 10-gate policy. If ALL gates pass, override
        # to TRIGGER. If ANY fails, fall through to the standard
        # soft-reject. ELITE keeps its default path (already triggers
        # alone via B7-A weight=1.0).
        try:
            from app.services.single_strong_policy import (
                SINGLE_STRONG_STRICT_POLICY_ENABLED,
                SingleStrongStrictContext,
                is_single_strong_strict_eligible,
            )
            if (
                SINGLE_STRONG_STRICT_POLICY_ENABLED
                and wallet_tier == "STRONG"
                and cluster_event.action in ("NEW_CLUSTER", "CONSENSUS_BUILDING")
            ):
                from app.models.market import Market as _Market
                _market_row = self.session.get(_Market, signal.market_id) if signal.market_id else None
                _capital_eur = float(self.paper_capital or 0.0)
                _spread_pct = float(audit_context.get("spread_pct") or 0.0)
                _ev_lb = float(audit_context.get("ev_lower_bound") or audit_context.get("EV_lower_bound") or 0.0)
                # ev_after_fee approximation using preset polymarket_fee_pct
                _ev_after_fee = _ev_lb - 0.04
                _edge_score = self._normalise_edge(audit_context.get("copyable_edge_score") or 0.0)
                ctx = SingleStrongStrictContext(
                    wallet_tier=wallet_tier,
                    capital_total_eur=_capital_eur,
                    market_speed_class=getattr(_market_row, "market_speed_class", None) if _market_row else None,
                    duration_bucket=getattr(_market_row, "duration_bucket", None) if _market_row else None,
                    category=(getattr(_market_row, "category", None) if _market_row else audit_context.get("category")),
                    deadline_known=bool(_market_row and getattr(_market_row, "deadline", None) is not None),
                    copyable_edge=_edge_score,
                    ev_after_fee=_ev_after_fee,
                    spread=_spread_pct,
                    orderbook_quality=audit_context.get("orderbook_quality"),
                )
                eligible, fail_reason = is_single_strong_strict_eligible(ctx)
                if eligible:
                    # Override the cluster decision: trigger the cluster now.
                    cluster_event.cluster.cluster_classification = "SINGLE_STRONG_STRICT_TRIGGER"
                    get_cluster_engine().mark_triggered(
                        cluster_event.cluster.cluster_id,
                        f"single_strong_strict_{signal.id[:12]}",
                    )
                    audit_context["_cluster_event"] = cluster_event
                    audit_context["_single_strong_strict_applied"] = True
                    return None
                # Fall-through: log fail_reason in cluster_details below
                audit_context["_single_strong_strict_fail_reason"] = fail_reason
        except Exception as exc:  # pragma: no cover — policy fail must not break pipeline
            logger.warning("single_strong_strict policy error: %s", exc)

        # Anything else is a soft-reject logged as no-trade.
        reason_code = CLUSTER_BLOCK_REASONS.get(
            cluster_event.action, "CLUSTER_REJECTED_OTHER"
        )
        cluster_details = {
            "cluster_id": cluster_event.cluster.cluster_id,
            "cluster_classification": cluster_event.cluster.cluster_classification,
            "consensus_score": cluster_event.cluster.consensus_score,
            "wallets_in_cluster": len(cluster_event.cluster.wallets_confirming),
            "cluster_action": cluster_event.action,
            "cluster_reason": cluster_event.reason,
            "cluster_state": cluster_event.cluster.state,
        }
        risk = RiskEngine()
        decision = RiskDecision(
            approved=False,
            reason=f"Cluster check: {cluster_event.action} — {cluster_event.reason}",
            reason_code=reason_code,
            details=cluster_details,
        )
        try:
            risk.log_no_trade(
                self.session,
                decision,
                signal_id=signal.id,
                market_id=signal.market_id,
                wallet_address=wallet_address or None,
                details=cluster_details,
            )
        except Exception as exc:  # pragma: no cover - logging shouldn't break the pipeline
            logger.warning("cluster log_no_trade failed: %s", exc)
        return {
            "executed": False,
            "reason": cluster_event.action,
            "reason_code": reason_code,
            "cluster": cluster_details,
        }

    def maybe_auto_trade(
        self,
        signal: Signal,
        audit_context: dict[str, Any],
        *,
        profile: RiskModeProfile | None = None,
    ) -> dict[str, Any]:
        if not self.settings.auto_paper_trade_enabled:
            return {"executed": False, "reason": "auto_paper_trade_disabled", "reason_code": "INSUFFICIENT_DATA"}
        if self._kill_switch_active():
            return {"executed": False, "reason": "kill_switch_active", "reason_code": "KILL_SWITCH"}
        if self._bot_mode() not in ("PAPER", "SEMI_AUTO"):
            return {"executed": False, "reason": "mode_not_paper", "reason_code": "INSUFFICIENT_DATA"}
        if signal.decision != "PAPER_TRADE":
            return {"executed": False, "reason": "signal_decision_not_paper", "reason_code": "LOW_SIGNAL_SCORE"}
        if self.has_open_position_for_signal(signal.id):
            return {"executed": False, "reason": "duplicate_signal_position", "reason_code": "TOO_MUCH_EXPOSURE"}
        wallet_for_signal = audit_context.get("wallet")
        if self.has_open_position_for_market_outcome(signal.market_id, signal.outcome, wallet_for_signal):
            return {"executed": False, "reason": "duplicate_market_outcome_position", "reason_code": "TOO_MUCH_EXPOSURE"}

        # P0-A 2026-05-18 fix bug #70 — single fresh capital read per decision.
        # Without this, self.paper_capital was cached at __init__ and stayed
        # stale even after +$57K PnL → exposure cap stuck on init value → bot
        # blocked in 1-in/1-out at ~$10K exposure while effective=$67K.
        #
        # Doctrine : ONE read of _effective_paper_capital() per decision via
        # the snapshot helper, then set self.paper_capital from snapshot so all
        # downstream helpers (current_exposure, market_exposure, _capital_available
        # _usd, _max_wallet_usd, PortfolioState.capital_total) see identical value.
        _effective = self._effective_paper_capital()  # ONE read
        _cap_snapshot = self._decision_capital_snapshot(effective_override=_effective)
        self.paper_capital = _effective  # propagate same value to all helpers

        # v0.7.2 — SignalClusterEngine check BEFORE allocator. Collapses
        # N wallet signals on the same (market, outcome, side) into one
        # logical trade decision. Filters DUPLICATE / CONFLICT / LATE_TRAP
        # / PENDING_CONSENSUS / EXPIRED before we ever evaluate sizing.
        cluster_event_result = self._cluster_check(signal, audit_context)
        if cluster_event_result is not None:
            return cluster_event_result

        active_profile = profile or get_active_profile(self.session)
        wallet_address = audit_context.get("wallet")
        wallet_record = self.lookup_wallet_record(wallet_address)
        candidate_status = audit_context.get("candidate_status") or (wallet_record.candidate_status if wallet_record else None)
        sample_size_ctx = audit_context.get("market_sample_size")
        market_sample_size = int(sample_size_ctx) if sample_size_ctx else (wallet_record.resolved_markets_traded if wallet_record else 0)
        confidence_ctx = audit_context.get("win_rate_confidence")
        win_rate_confidence = str(confidence_ctx) if confidence_ctx else (wallet_record.win_rate_confidence if wallet_record else "INSUFFICIENT_DATA")
        risk = RiskEngine()
        decision = risk.validate_for_mode(
            active_profile,
            candidate_status=candidate_status,
            market_sample_size=market_sample_size,
            win_rate_confidence=win_rate_confidence,
            signal_score=signal.score,
            spread=audit_context.get("spread_pct") or 0.0,
            liquidity=audit_context.get("liquidity") or 0.0,
            copyable_edge_score=audit_context.get("copyable_edge_score") or 0.0,
            orderbook_quality=str(audit_context.get("orderbook_quality") or "INSUFFICIENT_DATA"),
            copy_delay_seconds=audit_context.get("copy_delay_seconds") or 0.0,
            price_deterioration=audit_context.get("price_deterioration") or 0.0,
            total_exposure=self.current_exposure(),
            market_exposure=self.market_exposure(signal.market_id),
            wallet_exposure=self.wallet_exposure(wallet_address),
            open_positions_count=len(self.update_paper_positions()),
            daily_trades_count=self.daily_trade_count(),
            kill_switch_active=self._kill_switch_active(),
            capital_total=float(self.paper_capital),  # 2026-05-17 live capital for position_size
        )
        if not decision.approved:
            # P0-A : include capital snapshot in no-trade log for observability.
            # Operator can audit if MAX_TOTAL_EXPOSURE rejects make sense given
            # effective_capital and tier resolution at decision time.
            risk.log_no_trade(
                self.session,
                decision,
                signal_id=signal.id,
                market_id=signal.market_id,
                wallet_address=wallet_address,
                details={
                    "profile": active_profile.name,
                    "candidate_status": candidate_status,
                    "capital_snapshot": _cap_snapshot,
                    **(decision.details or {}),
                },
            )
            # A4 (2026-05-11): emit shadow log if strict mode rejected a trade
            # the legacy ELITE bypass would have skipped.
            self._maybe_log_shadow_bypass(
                reason_code=decision.reason_code,
                signal=signal,
                wallet_address=wallet_address,
                candidate_status=str(candidate_status or ""),
                audit_details={"profile": active_profile.name, "source": "risk_engine"},
            )
            return {
                "executed": False,
                "reason": decision.reason,
                "reason_code": decision.reason_code,
                "profile": active_profile.name,
                "candidate_status": candidate_status,
            }
        preset = get_preset(str(audit_context.get("allocator_preset") or self._allocator_preset_name(active_profile)))
        price = audit_context.get("price") or 0.0
        spread = audit_context.get("spread_pct") or 0.0
        copyable_edge_score = self._normalise_edge(
            audit_context.get("copyable_edge_score", signal.copyable_edge)
        )
        ev_lower_bound = audit_context.get("ev_lower_bound", audit_context.get("EV_lower_bound"))
        if ev_lower_bound is None:
            ev_lower_bound = signal.copyable_edge
        try:
            ev_lower_bound_f = float(ev_lower_bound or 0.0)
        except (TypeError, ValueError):
            ev_lower_bound_f = 0.0
        # 2026-05-11 (Option B fix UNKNOWN_CATEGORY root cause):
        # use the cascade resolver instead of the raw audit_context value.
        # Fallback chain: hint → Market.category → ResolvedMarketRecord.category
        # → infer_category(slug, question) → "Unknown". This eliminates ~62%
        # of paper trade rejections that were due to Market.category=NULL
        # while the category was actually known via other sources.
        from app.services.category_resolver import resolve_category_with_fallback
        _hint_cat = audit_context.get("category")
        category, _category_source = resolve_category_with_fallback(
            self.session, signal.market_id, hint_category=_hint_cat,
        )
        if not category:
            category = "Uncategorised"
        # Surface the resolution source for debugging / shadow-log audits
        audit_context["_category_resolution_source"] = _category_source
        fee_buffer, category_warning = category_fee_buffer(category)
        total_exposure_usd = self.open_notional_usd()
        capital_available = self._capital_available_usd()
        # v0.7.8 P3 — clamp the proposed size to the preset's per-wallet
        # / per-market caps BEFORE the allocator evaluates. Without this,
        # signals carrying the WALLET'S original notional (e.g. $8) get
        # rejected at small capital (cap=$5.4 at 50€) even though the
        # bot would happily open a smaller copy. The allocator then
        # sizes the actual position based on risk_per_trade.
        _max_wallet_usd = float(self.paper_capital) * float(preset.max_wallet_exposure)
        _max_market_usd = float(self.paper_capital) * float(preset.max_market_exposure)
        _proposed_size = float(signal.proposed_size_usd or 0.0)
        _clamped_size = min(_proposed_size, _max_wallet_usd, _max_market_usd) if _proposed_size > 0 else 0.0
        allocator_signal = TradeSignal(
            wallet_address=wallet_address or "",
            wallet_tier=self.resolve_effective_tier(audit_context, wallet_record),
            market_id=signal.market_id,
            side=(audit_context.get("side") or "BUY").upper(),
            price=float(price or 0.0),
            size=_clamped_size,
            notional_usd=_clamped_size,
            ev_lower_bound=ev_lower_bound_f,
            copyable_edge=copyable_edge_score,
            spread=float(spread or 0.0),
            liquidity=float(audit_context.get("liquidity") or 0.0),
            # v0.7.2 B1 — preferred 0-100 score gate. The audit pipeline puts
            # the score in audit_context["liquidity_score"] when available; if
            # not, fall back to the legacy "liquidity_score" key on
            # tradeauditrecord (named market_liquidity_score upstream).
            liquidity_score=(
                float(audit_context["liquidity_score"])
                if audit_context.get("liquidity_score") is not None
                else (
                    float(audit_context["market_liquidity_score"])
                    if audit_context.get("market_liquidity_score") is not None
                    else None
                )
            ),
            orderbook_quality=str(audit_context.get("orderbook_quality") or "INSUFFICIENT_DATA"),
            category=category,
            copy_delay_seconds=float(audit_context.get("copy_delay_seconds") or 0.0),
            price_deterioration=float(audit_context.get("price_deterioration") or 0.0),
            # v0.7.8 P3 — fetch expected_holding_time from market metadata
            # so the allocator's UNKNOWN_DEADLINE / B13 duration gates see
            # real values. Without this, signals were always None →
            # all small-capital trades rejected.
            expected_holding_time_hours=self._lookup_expected_holding_hours(signal.market_id),
        )
        # v0.7.8 P6 — R-based dynamic sizing: read current multiplier from
        # BotState (anti-drawdown state machine: 2.0 winning, 1.0 losing).
        _bot_state = self.session.get(BotState, 1)
        _current_r = (
            float(_bot_state.current_r_multiplier)
            if _bot_state and _bot_state.current_r_multiplier is not None
            else 2.0
        )
        allocator_state = PortfolioState(
            capital_total=float(self.paper_capital),
            capital_available=capital_available,
            kill_switch=self._kill_switch_active(),
            exposure_total=total_exposure_usd,
            exposure_per_wallet={wallet_address or "": self.wallet_exposure_usd(wallet_address)},
            exposure_per_market={signal.market_id: self.market_exposure_usd(signal.market_id)},
            open_positions_count=len(self.update_paper_positions()),
            execution_mode="PAPER",
            live_enabled=False,
            min_order_size_status="unverified",
            current_r_multiplier=_current_r,
        )
        allocator_decision = CapitalAllocator().evaluate_trade(allocator_signal, allocator_state, preset)
        allocator_details = {
            "allocator_required": True,
            "allocator_preset": preset.name,
            "wallet": wallet_address,
            "tier": allocator_signal.wallet_tier,
            "status": candidate_status,
            "mode": active_profile.name,
            "execution_mode": "PAPER",
            "capital_total": allocator_state.capital_total,
            "capital_available": allocator_state.capital_available,
            "stake_size": allocator_decision.sizing,
            "min_order_size": preset.min_stake,
            "min_order_size_status": allocator_state.min_order_size_status,
            "ev_lower_bound": ev_lower_bound_f,
            "fee_buffer": fee_buffer,
            "category": category,
            "category_warning": category_warning,
            "copyable_edge_score": copyable_edge_score,
            "spread": spread,
            "liquidity": audit_context.get("liquidity") or 0.0,
            "exposure_before": total_exposure_usd,
            "exposure_after": allocator_decision.exposure_after_trade,
            "capital_after_trade": allocator_decision.capital_after_trade,
            "reason_code": allocator_decision.reason_code,
            "next_bottleneck": allocator_decision.next_bottleneck,
            "allocator_reasoning": allocator_decision.reasoning,
        }
        if allocator_decision.rejected:
            self._log_allocator_no_trade(
                risk,
                allocator_decision,
                signal,
                wallet_address,
                allocator_details,
            )
            return {
                "executed": False,
                "reason": f"allocator_rejected_{allocator_decision.reason_code}",
                "reason_code": allocator_decision.reason_code,
                "profile": active_profile.name,
                "allocator_preset": preset.name,
                "candidate_status": candidate_status,
                "allocator": allocator_details,
            }
        # 2026-05-17 : remove single-R cap (self.paper_capital × max_risk_per_trade)
        # qui réduisait allocator 2R → 1R. Allocator est seul authority pour R-based
        # sizing (state machine 2R win / 1R loss), avec safeguards :
        # - min_stake floor (preset BASE_PAPER $5)
        # - wallet/market exposure caps (compute_sizing line 1081)
        # - max_total_exposure (allocator gate)
        size_usd = min(
            signal.proposed_size_usd or 0.0,
            decision.position_size,
            allocator_decision.sizing,
        )
        if size_usd <= 0:
            return {"executed": False, "reason": "size_zero", "reason_code": "INSUFFICIENT_DATA", "profile": active_profile.name}
        side = (audit_context.get("side") or "BUY").upper()

        # P0.3 (Round 8 2026-05-12) — M1 v5 VISIBILITY GATE.
        # Refuse trades that are causally impossible: we cannot have copied a
        # source trade we hadn't SEEN yet. If the source PublicTrade.seen_at is
        # later than now (the bot's open moment), it means our polling somehow
        # produced a signal before observing the trade — likely a wrong join,
        # backfill, or clock skew. Either way, live cannot reproduce it.
        #
        # We use a small tolerance to absorb legitimate clock jitter (1-2s).
        # IMPORTANT: this gate is NOT about copy delay (which can be 100s+
        # legitimately). It only blocks causality violations. DELAYED_POLL
        # trades remain accepted — they are just measured for live-readiness
        # downstream by M1 v5 metric splits.
        _source_tid = audit_context.get("source_trade_id")
        if _source_tid:
            try:
                from app.models.trade import PublicTrade as _PT
                _src_row = self.session.get(_PT, _source_tid)
                if _src_row is not None and _src_row.seen_at is not None:
                    _seen = _src_row.seen_at
                    if _seen.tzinfo is None:
                        _seen = _seen.replace(tzinfo=UTC)
                    _now = datetime.now(UTC)
                    _delta_s = (_seen - _now).total_seconds()
                    # If seen_at is in the future relative to now → causality
                    # violation. Tolerance 2s for jitter.
                    if _delta_s > 2.0:
                        self._log_visibility_leakage(
                            signal=signal, audit_context=audit_context,
                            source_trade_id=_source_tid,
                            source_seen_at=_seen, now_utc=_now, delta_s=_delta_s,
                        )
                        return {
                            "executed": False,
                            "reason": "visibility_leakage_suspect",
                            "reason_code": "VISIBILITY_LEAKAGE_SUSPECT",
                            "profile": active_profile.name,
                            "source_seen_at": _seen.isoformat(),
                            "delta_seconds_after_now": round(_delta_s, 3),
                        }
            except Exception:
                # Defence: NEVER block a trade on the gate's own failure.
                # The gate is best-effort; missing source row means we cannot
                # verify but also cannot prove violation.
                pass

        # v0.7.8 — re-fetch live market price at open time. This closes a
        # structural optimism in the paper PnL: opening at the wallet's
        # frozen traded-at price assumes zero execution latency, which is
        # never the case (we poll at 90s + Polymarket round-trip). On
        # ULTRA_SHORT markets the price moves 10-30% in 90s, so the
        # wallet price is not what a real order would fill at.
        wallet_signal_price = float(price or 0.0)
        execution_price, exec_source = self._fetch_execution_price_now(
            signal.market_id, signal.outcome, side,
        )
        if exec_source == "market_resolved":
            # Market settled between wallet trade and our open — skip.
            return {
                "executed": False,
                "reason": "market_resolved_before_open",
                "reason_code": "INSUFFICIENT_DATA",
                "profile": active_profile.name,
                "wallet_signal_price": wallet_signal_price,
            }
        if execution_price is None or execution_price <= 0.0:
            # Gamma unavailable / outcome not found — fall back to wallet
            # price so we don't lose the trade entirely, but flag the
            # source explicitly so the operator sees the bias risk.
            execution_price = wallet_signal_price
            exec_source = "wallet_frozen_fallback"

        latency_drift = round(execution_price - wallet_signal_price, 6)

        # P0.4 (Round 8): build the entry-price provenance dict for EntryPriceAudit.
        # We capture everything we have at this point:
        #   - raw_signal_price = wallet's traded price (source we copied)
        #   - gamma_mid_at_open = the re-fetched live Gamma price
        #   - entry_price_source = where the chosen price comes from
        #   - confidence based on whether re-fetch succeeded
        # CLOB best_bid/ask remain None — Polymarket CLOB is 404 on the crypto 5min
        # markets that dominate our flow. live_shadow is computed from gamma+spread/2.
        _audit_data: dict = {
            "raw_signal_price": wallet_signal_price,
            "gamma_mid_at_open": execution_price if exec_source == "gamma_refetched" else None,
            "clob_best_bid_at_open": None,
            "clob_best_ask_at_open": None,
            "spread_at_open": spread,
            "liquidity_score_at_open": audit_context.get("liquidity_score"),
            "entry_price_source": (
                "GAMMA" if exec_source == "gamma_refetched"
                else "WALLET_FROZEN_FALLBACK" if exec_source == "wallet_frozen_fallback"
                else "UNKNOWN"
            ),
            "price_source_confidence": (
                "HIGH" if exec_source == "gamma_refetched"
                else "LOW"
            ),
            "source_trade_id": audit_context.get("source_trade_id"),
            "source_wallet": audit_context.get("wallet"),
        }

        # Use the LIVE price for sizing the position.
        quantity = size_usd / max(execution_price or 1, 0.01)

        # 2026-05-17 — LIVE bridge : si LIVE_ENABLED=true, send vrai ordre CLOB.
        # Sinon, paper comme avant. MÊME logique de décision en amont (signals,
        # audit, cluster, R-sizing) — seule l'exécution change.
        try:
            from app.services.live_executor_bridge import decide_and_place_live
            live_result = decide_and_place_live(
                session=self.session,
                market_id=signal.market_id,
                outcome=signal.outcome,
                side=side.upper(),
                price=execution_price,
                notional_usd=size_usd,
                signal_id=signal.id,
                wallet_address=audit_context.get("wallet"),
            )
        except Exception as _e:
            # Bridge totally broken — log + fallback paper to not crash bot
            try:
                import logging as _lg
                _lg.getLogger(__name__).error(f"live bridge error: {type(_e).__name__}: {_e}")
            except Exception:
                pass
            live_result = {"mode": "paper", "success": True, "skip_reason": "BRIDGE_ERROR"}

        if live_result.get("mode") == "skip":
            # LIVE failed (rate limit, CLOB reject, token unresolvable). NE PAS
            # fallback paper (= éviter divergence comptable paper/live).
            return {
                "executed": False,
                "reason": f"LIVE_SKIP: {live_result.get('skip_reason')}",
                "reason_code": "LIVE_REJECT",
                "error": live_result.get("error"),
            }

        # Tag audit_data with live info (if any)
        if live_result.get("mode") == "live":
            _audit_data["live_executed"] = True
            _audit_data["live_order_id"] = live_result.get("order_id")
            _audit_data["live_fill_price"] = live_result.get("fill_price")

        trade = self.open_paper_position(
            market_id=signal.market_id,
            outcome=signal.outcome,
            side=side,
            quantity=quantity,
            price=execution_price,
            spread=spread,
            signal_id=signal.id,
            wallet_address=audit_context.get("wallet"),
            auto=True,
            notional_usd=size_usd,
            allocator_checked=True,
            entry_audit=_audit_data,
        )

        # v0.7.8 Phase 2 — register the new position with the adaptive
        # close scheduler so it gets re-checked at a cadence appropriate
        # to its duration bucket (ULTRA_SHORT = 20s, VERY_SHORT = 60s,
        # etc). Replaces the fixed 600s polling that locked $5 of capital
        # for 10 min on a 1MN trade.
        try:
            from app.services.adaptive_close_scheduler import get_scheduler
            from app.models.market import Market as _Market
            _market_row = self.session.get(_Market, signal.market_id) if signal.market_id else None
            _bucket = getattr(_market_row, "duration_bucket", None) if _market_row else None
            get_scheduler().register_position(
                position_id=trade.id,
                market_id=signal.market_id or "",
                duration_bucket=_bucket,
            )
        except Exception:
            # Scheduler is best-effort; if it fails, the legacy 600s
            # close-loop still acts as a safety net.
            pass

        # Audit trail: the bias delta belongs in the cluster_event payload
        # so the close-reporting + back-tests can quantify how much paper
        # PnL drifts from a "would have copied at wallet price" baseline.
        try:
            audit_context["execution_price_source"] = exec_source
            audit_context["execution_price"] = round(execution_price, 6)
            audit_context["wallet_signal_price"] = round(wallet_signal_price, 6)
            audit_context["execution_latency_drift"] = latency_drift
        except Exception:
            pass
        # v0.7.2 — mark cluster TRIGGERED so subsequent signals on the same
        # (market, outcome, side) become DUPLICATE / PYRAMID candidates.
        cluster_pending = audit_context.get("_cluster_event")
        if cluster_pending is not None:
            try:
                get_cluster_engine().mark_triggered(
                    cluster_pending.cluster.cluster_id, trade.id
                )
            except Exception as exc:  # pragma: no cover - safety net
                logger.warning("cluster mark_triggered failed: %s", exc)
        return {
            "executed": True,
            "trade_id": trade.id,
            "size_usd": size_usd,
            "profile": active_profile.name,
            "allocator_preset": preset.name,
            "allocator": allocator_details,
            "cluster": (
                {
                    "cluster_id": cluster_pending.cluster.cluster_id,
                    "classification": cluster_pending.cluster.cluster_classification,
                    "consensus_score": cluster_pending.cluster.consensus_score,
                    "wallets_in_cluster": len(cluster_pending.cluster.wallets_confirming),
                    "action": cluster_pending.action,
                    "pyramid": cluster_pending.action == ACTION_PYRAMID_OK,
                }
                if cluster_pending is not None
                else None
            ),
        }

    def close_position_by_id(self, position_id: str, exit_price: float, reason: str | None = None) -> PaperTrade | None:
        trade = self.session.get(PaperTrade, position_id)
        if trade is None or trade.status == "closed":
            return None
        return self.close_paper_position(trade, exit_price, reason=reason)

    def evaluate_open_positions(self, price_lookup: dict[str, float] | None = None) -> list[dict[str, Any]]:  # noqa: C901
        def _aware(dt: datetime) -> datetime:
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

        """Close open positions on market resolution, take-profit, stop-loss,
        or max-holding-time. Resolution-based close beats price-based —
        a resolved market is canonical exit at 1.0 (winner) or 0.0 (loser).
        """
        events: list[dict[str, Any]] = []
        price_lookup = price_lookup or {}
        for trade in self.update_paper_positions():
            # 1. Market resolution close — canonical exit when known.
            resolution = self._resolve_market(trade.market_id)
            if resolution is not None:
                exit_price = self._resolution_exit_price(trade, resolution)
                if exit_price is not None:
                    self.close_position_by_id(trade.id, exit_price, reason="market_resolved")
                    events.append({
                        "trade_id": trade.id,
                        "action": "market_resolved",
                        "exit": exit_price,
                        "winning_outcome": resolution.winning_outcome_name,
                    })
                    continue
            # 2. Price-based close paths.
            current = price_lookup.get(trade.market_id)
            if current is None:
                # Without a current price we still respect max-holding-time using
                # the entry price as a stand-in (forces a close even if data is
                # stale). This guarantees positions don't pile up forever.
                if trade.opened_at and (datetime.now(UTC) - _aware(trade.opened_at)).days >= 7:
                    self.close_position_by_id(trade.id, trade.average_price, reason="max_holding_time_no_price")
                    events.append({"trade_id": trade.id, "action": "max_holding_time_no_price", "exit": trade.average_price})
                continue
            if trade.side.upper() == "BUY":
                pnl_pct = (current - trade.average_price) / max(trade.average_price, 0.01)
            else:
                pnl_pct = (trade.average_price - current) / max(trade.average_price, 0.01)
            if pnl_pct >= 0.10:
                self.close_position_by_id(trade.id, current, reason="take_profit")
                events.append({"trade_id": trade.id, "action": "take_profit", "exit": current})
            elif pnl_pct <= -0.05:
                self.close_position_by_id(trade.id, current, reason="stop_loss")
                events.append({"trade_id": trade.id, "action": "stop_loss", "exit": current})
            elif trade.opened_at and (datetime.now(UTC) - _aware(trade.opened_at)).days >= 7:
                self.close_position_by_id(trade.id, current, reason="max_holding_time")
                events.append({"trade_id": trade.id, "action": "max_holding_time", "exit": current})
        return events

    # ---------------- resolution helpers ----------------

    def _resolve_market(self, market_id: str | None) -> ResolvedMarketRecord | None:
        if not market_id:
            return None
        # ResolvedMarketRecord is keyed by market_id (which equals condition_id
        # in the discovery pipeline). Fall back to a lookup by condition_id.
        rec = self.session.get(ResolvedMarketRecord, market_id)
        if rec and rec.usable and rec.winning_outcome_index is not None:
            return rec
        try:
            rec = self.session.exec(
                select(ResolvedMarketRecord).where(
                    ResolvedMarketRecord.condition_id == market_id
                )
            ).first()
        except Exception:
            rec = None
        if rec and rec.usable and rec.winning_outcome_index is not None:
            return rec
        return None

    def _resolution_exit_price(
        self,
        trade: PaperTrade,
        resolution: ResolvedMarketRecord,
    ) -> float | None:
        """Map (trade.outcome, resolution.winning_outcome_*) to the exit price."""
        if resolution.winning_outcome_index is None:
            return None
        # Outcome may be stored as the name ("Yes"/"No") or as the index string.
        outcome = (trade.outcome or "").strip()
        if not outcome:
            return None
        winning_name = (resolution.winning_outcome_name or "").strip().lower()
        winning_index = int(resolution.winning_outcome_index)
        if outcome.lower() == winning_name:
            return 1.0
        try:
            if int(outcome) == winning_index:
                return 1.0
        except (TypeError, ValueError):
            pass
        return 0.0

    def performance(self) -> dict[str, Any]:
        report = self.generate_paper_report()
        report["daily_pnl"] = self.daily_pnl()
        report["weekly_pnl"] = self.weekly_pnl()
        report["exposure"] = self.current_exposure()
        report["capital"] = self.paper_capital
        report["auto_enabled"] = self.settings.auto_paper_trade_enabled
        return report
