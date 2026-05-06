from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.database import get_session
from app.services.candidate_validation_service import CandidateValidationService
from app.services.market_first_discovery import MarketFirstDiscoveryService
from app.services.validated_paper_universe import ValidatedPaperUniverse

router = APIRouter(prefix="/discovery", tags=["discovery"])


class MarketFirstRunRequest(BaseModel):
    days_back: int | None = None
    max_markets: int | None = None
    trades_per_market: int | None = None


class CandidateValidationRequest(BaseModel):
    days_back: int | None = None
    max_markets: int | None = None
    trades_per_market: int | None = None
    split_ratio: float | None = None


@router.get("/market-first/status")
def market_first_status(session: Session = Depends(get_session)) -> dict:
    return MarketFirstDiscoveryService(session).status()


@router.get("/market-first/report")
def market_first_report(session: Session = Depends(get_session)) -> dict:
    service = MarketFirstDiscoveryService(session)
    latest = service.latest_report()
    if latest is None:
        return {
            "available": False,
            "message": "No market-first discovery run yet. POST /discovery/market-first/run to produce one.",
            "exports": service.export_paths(),
        }
    return latest


@router.get("/market-first/run")
def market_first_run_idempotent(session: Session = Depends(get_session)) -> dict:
    """Convenience GET that returns the latest report without triggering a run."""
    return market_first_report(session=session)


@router.post("/market-first/run")
def market_first_run(payload: MarketFirstRunRequest | None = None, session: Session = Depends(get_session)) -> dict:
    payload = payload or MarketFirstRunRequest()
    report = MarketFirstDiscoveryService(session).run(
        days_back=payload.days_back,
        max_markets=payload.max_markets,
        trades_per_market=payload.trades_per_market,
    )
    return report.to_dict()


@router.get("/market-first/wallets")
def market_first_wallets(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    return MarketFirstDiscoveryService(session).list_top_wallets(limit=limit)


@router.get("/market-first/markets")
def market_first_markets(limit: int = 200, usable_only: bool = False, session: Session = Depends(get_session)) -> list[dict]:
    return MarketFirstDiscoveryService(session).list_markets(usable_only=usable_only, limit=limit)


@router.get("/market-first/rejected-markets")
def market_first_rejected_markets(limit: int = 200, session: Session = Depends(get_session)) -> list[dict]:
    return MarketFirstDiscoveryService(session).list_rejected_markets(limit=limit)


@router.get("/market-first/export")
def market_first_export(session: Session = Depends(get_session)) -> dict:
    return MarketFirstDiscoveryService(session).export_paths()


# ---------------- v0.5.1 candidate validation ----------------


@router.post("/market-first/validate")
def market_first_validate(
    payload: CandidateValidationRequest | None = None,
    session: Session = Depends(get_session),
) -> dict:
    payload = payload or CandidateValidationRequest()
    service = CandidateValidationService(session)
    report = service.run_validation(
        days_back=payload.days_back or 365,
        max_markets=payload.max_markets or 300,
        trades_per_market=payload.trades_per_market,
        split_ratio=payload.split_ratio or 0.7,
    )
    return report.to_dict()


@router.get("/market-first/validate")
def market_first_validate_latest(session: Session = Depends(get_session)) -> dict:
    service = CandidateValidationService(session)
    latest = service.latest_report()
    if latest is None:
        return {
            "available": False,
            "message": "No validation run yet. POST /discovery/market-first/validate to produce one.",
            "exports": service.export_paths(),
        }
    return latest


@router.get("/market-first/validate/export")
def market_first_validate_export(session: Session = Depends(get_session)) -> dict:
    return CandidateValidationService(session).export_paths()


# ---------------- v0.7.8 P5 — manual weekly reclass trigger ----------------
# Operator wants a UI button to re-run the weekly reclass on demand instead
# of waiting for the Sunday 02:00 UTC cron. Useful when the cohort needs an
# immediate refresh (e.g. post-discovery batch, post-spec change).


class WeeklyReclassRequest(BaseModel):
    dry_run: bool = False  # if True, compute decisions but don't write


@router.post("/reclass/run")
def run_reclass(payload: WeeklyReclassRequest, session: Session = Depends(get_session)) -> dict:
    """Trigger a one-shot weekly reclass. NEVER throws — failures captured
    in the response (rolled_back / errors)."""
    from app.services.weekly_reclass_service import run_weekly_reclass, reclass_summary_md
    from app.config import get_settings
    settings = get_settings()
    db_path = settings.resolved_sqlite_path
    backup_dir = db_path.parent / "_reclass_backups"
    result = run_weekly_reclass(
        session,
        db_path=db_path,
        backup_dir=backup_dir,
        dry_run=payload.dry_run,
    )
    return {
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "cohort_before": result.cohort_before,
        "cohort_after": result.cohort_after,
        "promoted_count": len(result.promoted),
        "demoted_count": len(result.demoted),
        "unchanged": result.unchanged,
        "rolled_back": result.rolled_back,
        "errors": result.errors,
        "db_backup_path": result.db_backup_path,
        "summary_md": reclass_summary_md(result),
        "promoted": [
            {"address": d.address, "previous": d.previous_status, "new": d.new_status, "reason": d.reason}
            for d in result.promoted
        ],
        "demoted": [
            {"address": d.address, "previous": d.previous_status, "new": d.new_status, "reason": d.reason}
            for d in result.demoted
        ],
    }


@router.get("/reclass/promotion-candidates")
def get_promotion_candidates(session: Session = Depends(get_session)) -> dict:
    """v0.7.8 P6 — surface STRONG wallets that are 1-30 trades from ELITE
    (resolved_W+L in [70,99] AND wr >= 0.90). Operator wants to know who's
    on the cusp of promotion."""
    from app.models.wallet import MarketFirstWalletRecord
    from sqlmodel import select, or_, and_
    rows = session.exec(
        select(MarketFirstWalletRecord)
        .where(MarketFirstWalletRecord.candidate_status == "STRONG")
        .where(MarketFirstWalletRecord.resolved_market_win_rate >= 0.90)
    ).all()
    candidates = []
    for r in rows:
        wins = int(r.resolved_winning_markets or 0)
        losses = int(r.resolved_losing_markets or 0)
        wl = wins + losses
        if 70 <= wl < 100:
            candidates.append({
                "address": r.address,
                "wins": wins,
                "losses": losses,
                "wins_plus_losses": wl,
                "win_rate": float(r.resolved_market_win_rate or 0),
                "trades_to_elite": 100 - wl,
            })
    candidates.sort(key=lambda c: (c["win_rate"], c["wins_plus_losses"]), reverse=True)
    return {
        "count": len(candidates),
        "candidates": candidates[:100],  # top 100 for display
    }


# ---------------- v0.5.4 validated paper universe ----------------


class UniverseMergeRequest(BaseModel):
    sources: list[dict] | None = None  # [{"label": "730d", "path": ".../candidate_elite_validation_report_730d.json"}]
    suffix: str | None = None


@router.get("/universe/latest")
def universe_latest() -> dict:
    universe = ValidatedPaperUniverse()
    summary = universe.latest_summary()
    if summary is None:
        return {
            "available": False,
            "message": "No universe built yet. POST /discovery/universe/merge after running validations.",
            "latest_csv_path": str(universe.latest_csv_path),
        }
    return summary


@router.post("/universe/merge")
def universe_merge(payload: UniverseMergeRequest | None = None) -> dict:
    payload = payload or UniverseMergeRequest()
    universe = ValidatedPaperUniverse()
    sources: list[tuple[str, Path]]
    if payload.sources:
        sources = [(item["label"], Path(item["path"])) for item in payload.sources if item.get("path")]
    else:
        # Default: merge the canonical 730d + 1095d validation reports if present.
        exports = universe.exports_dir
        sources = []
        for label, filename in (("730d", "candidate_elite_validation_report_730d.json"), ("1095d", "candidate_elite_validation_report_1095d_3000.json")):
            candidate = exports / filename
            if candidate.exists():
                sources.append((label, candidate))
    if not sources:
        raise HTTPException(status_code=400, detail="No validation reports available to merge")
    summary = universe.merge(sources, suffix=payload.suffix or "")
    return summary.to_dict()
