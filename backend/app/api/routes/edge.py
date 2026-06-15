from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session
from app.services.edge_validation_engine import EdgeValidationEngine

router = APIRouter(prefix="/edge", tags=["edge"])


@router.get("/report")
def get_edge_report(session: Session = Depends(get_session)) -> dict:
    return EdgeValidationEngine(session).generate_edge_report()


@router.get("/metrics")
def get_edge_metrics(session: Session = Depends(get_session)) -> dict:
    return EdgeValidationEngine(session).metrics().__dict__


@router.get("/strategies")
def get_strategies(session: Session = Depends(get_session)) -> dict:
    return EdgeValidationEngine(session).compare_strategies()


@router.get("/wallets")
def get_wallet_breakdown(session: Session = Depends(get_session)) -> list[dict]:
    return EdgeValidationEngine(session).wallet_breakdown()


@router.get("/categories")
def get_category_breakdown(session: Session = Depends(get_session)) -> list[dict]:
    return EdgeValidationEngine(session).category_breakdown()


@router.get("/no-trade-log")
def get_no_trade_log(limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    return EdgeValidationEngine(session).no_trade_decision_log(limit=limit)


@router.get("/copy-efficiency")
def get_copy_efficiency(
    window: str = "24h",
    session: Session = Depends(get_session),
) -> dict:
    """M1 v2 copy_efficiency report (DEPRECATED — use /copy-efficiency-v3).

    v2 status: M1_V2_FIXED_NEGATIVE_BUG_BUT_RATIO_NEEDS_VALIDATION.
    Round 6 review (2026-05-12) flagged the 'treat SELL as BUY' simplification
    as unacceptable. Kept for back-compat / dashboard until UI cuts over."""
    from app.services.copy_efficiency_engine import copy_efficiency_payload
    return copy_efficiency_payload(session, window=window)


@router.get("/copy-efficiency-v3")
def get_copy_efficiency_v3(
    window_hours: float = 24.0,
    write_csv: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """M1 v3 forensic-grade copy_efficiency (Round 6 review — 2026-05-12).

    Splits the suspicious v2 global_ratio=10.03 into 5 distinct metrics:
    - join_quality_score (% exact_match wallet+market+side+outcome)
    - side_mapping_quality (% same_direction AND same_outcome)
    - copy_entry_efficiency (entry price quality vs source)
    - copy_resolved_efficiency (bot PnL/$ vs source counterfactual PnL/$)
    - source_realized_efficiency (diagnostic only)

    SELL handling: classified as BUY_ENTRY / SELL_EXIT / SELL_SHORT_OPEN /
    AMBIGUOUS by inspecting wallet's prior trades on the market. SELL_EXIT is
    EXCLUDED from copy_entry/resolved metrics (source closed long, bot opened
    new short — not comparable).

    Live gates (ALL must pass):
    - join_quality_score ≥ 0.95
    - side_mapping_quality ≥ 0.95
    - copy_entry_efficiency ≥ 0.70
    - copy_resolved_efficiency in [0.70, 2.5]
    - ambiguous_source_side_rate ≤ 0.05

    write_csv=true → exports per-trade forensic record to data/exports/.
    """
    from app.services.copy_efficiency_v3 import m1_v3_payload
    return m1_v3_payload(session, window_hours=window_hours, write_csv=write_csv)


@router.get("/growth-metrics")
def get_growth_metrics(
    window_hours: float = 168.0,
    session: Session = Depends(get_session),
) -> dict:
    """Growth health (Phase A complement — 2026-05-11).

    Returns CAGR + Sharpe + Sortino + Calmar + max_drawdown_pct +
    days_under_water + classification HEALTHY/WATCH/DEGRADED/CRITICAL.
    Filtre trades opened ≥ strict_cutover_at si cutover set."""
    from app.services.growth_metrics_engine import growth_metrics_payload
    return growth_metrics_payload(session, window_hours=window_hours)


@router.get("/wallet-weights")
def get_wallet_weights(
    window_days: int = 14,
    paper_mode: bool = True,
    session: Session = Depends(get_session),
) -> dict:
    """M3 wallet weighting (Phase C C1 — 2026-05-11).

    Returns wallet weights with formula 0.35×prior + 0.25×EWMA_14d +
    0.15×copy_eff + 0.10×consistency + 0.10×bucket_edge + 0.05×activity -
    penalties. Bayesian regularization on n_recent. Cap [0.5, 1.5] paper
    or [0.7, 1.25] live initial.

    NOT activated in runtime sizing yet — read-only preview for operator."""
    from app.services.wallet_weighting_engine import wallet_weights_payload
    return wallet_weights_payload(
        session, window_days=window_days, paper_mode=paper_mode
    )


@router.get("/category-breakdown-resolved")
def category_breakdown_resolved(session: Session = Depends(get_session)) -> dict:
    """P0.5 (Round 4 review — 2026-05-11): breakdown by RESOLVED category.

    The legacy /edge/categories shows Market.category which is often NULL/Unknown.
    This endpoint extracts the resolved_category from PaperTrade.close_reason
    metadata (P0.5 propagation), giving the TRUE category after the resolver
    fallback chain ran. Phase B baseline metrics use this view."""
    import json
    from collections import defaultdict
    from sqlmodel import select as _select
    from app.models.trade import PaperTrade
    from app.services.baseline_constants import EFFECTIVE_BASELINE_T0_ISO

    by_cat: dict[str, dict] = defaultdict(
        lambda: {"n_closed": 0, "pnl": 0.0, "wins": 0, "losses": 0, "sources": defaultdict(int)}
    )
    raw_counts = defaultdict(int)
    for trade in session.exec(
        _select(PaperTrade).where(PaperTrade.status == "closed")
    ).all():
        if not trade.close_reason or "|" not in trade.close_reason:
            raw_counts["legacy_no_metadata"] += 1
            continue
        try:
            _reason, payload = trade.close_reason.split("|", 1)
            meta = json.loads(payload)
        except Exception:
            raw_counts["parse_fail"] += 1
            continue
        resolved = meta.get("resolved_category") or meta.get("category") or "Unknown"
        source = meta.get("category_source") or "unknown"
        pnl = float(trade.realized_pnl or 0.0)
        bucket = by_cat[resolved]
        bucket["n_closed"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
        bucket["sources"][source] += 1

    out = []
    for cat, b in sorted(by_cat.items(), key=lambda kv: kv[1]["n_closed"], reverse=True):
        wr = b["wins"] / b["n_closed"] if b["n_closed"] else None
        out.append({
            "category": cat,
            "n_closed": b["n_closed"],
            "pnl": round(b["pnl"], 4),
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate": round(wr, 4) if wr is not None else None,
            "sources": dict(b["sources"]),
        })
    return {
        "effective_baseline_t0": EFFECTIVE_BASELINE_T0_ISO,
        "breakdown": out,
        "diagnostics": dict(raw_counts),
    }
