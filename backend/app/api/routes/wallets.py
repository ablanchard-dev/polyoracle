from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.database import get_session
from app.services.discovery_audit_service import DiscoveryAuditService
from app.services.market_first_discovery import MarketFirstDiscoveryService
from app.services.smart_wallet_auditor import SmartWalletAuditor
from app.services.win_rate_engine import WinRateEngine

router = APIRouter(tags=["wallets"])


class WalletAction(BaseModel):
    address: str
    reason: str | None = None


class WalletAuditRequest(BaseModel):
    address: str


class WalletAuditBatchRequest(BaseModel):
    addresses: list[str] | None = None
    limit: int | None = None


class DiscoveryAuditRequest(BaseModel):
    limit: int | None = None
    batch_size: int | None = None


@router.get("/wallets/top")
def get_top_wallets(limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    return SmartWalletAuditor(session).discover_top_wallets(limit=limit)


@router.get("/wallets/audited")
def get_audited_wallets(limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    auditor = SmartWalletAuditor(session)
    return [
        {
            "address": audit.address,
            "smart_score": audit.smart_score,
            "whale_score": audit.whale_score,
            "reliability": audit.reliability,
            "copyability": audit.copyability,
            "confidence": audit.confidence,
            "tier": audit.tier,
            "specialty": audit.specialty,
            "pnl": audit.pnl,
            "roi": audit.roi,
            "win_rate": audit.win_rate,
            "sample_size": audit.sample_size,
            "suspicious": audit.suspicious,
            "data_source": audit.data_source,
            "audit_at": audit.audit_at.isoformat() if audit.audit_at else None,
        }
        for audit in auditor.list_audited_wallets(limit=limit)
    ]


@router.get("/wallets/watchlist")
def get_watchlist(session: Session = Depends(get_session)) -> list[dict]:
    return SmartWalletAuditor(session).list_watchlist()


@router.post("/wallets/watchlist")
def add_watchlist(payload: WalletAction, session: Session = Depends(get_session)) -> dict[str, str]:
    SmartWalletAuditor(session)._upsert_watch_entry(payload.address, "watch", payload.reason or "manual")
    return {"address": payload.address, "status": "watched"}


@router.post("/wallets/blacklist")
def blacklist_wallet(payload: WalletAction, session: Session = Depends(get_session)) -> dict[str, str]:
    SmartWalletAuditor(session)._upsert_watch_entry(payload.address, "blacklist", payload.reason or "manual")
    return {"address": payload.address, "status": "blacklist"}


@router.get("/wallets/stats")
def wallets_stats(session: Session = Depends(get_session)) -> dict:
    return SmartWalletAuditor(session).stats()


# ---------------- v0.4.2 discovery + win rate ----------------


@router.post("/wallets/discovery/audit")
def run_discovery_audit(
    payload: DiscoveryAuditRequest | None = None,
    session: Session = Depends(get_session),
) -> dict:
    payload = payload or DiscoveryAuditRequest()
    limit = payload.limit or 100
    batch = payload.batch_size or 25
    report = DiscoveryAuditService(session).run_audit(limit=limit, batch_size=batch)
    return report.to_dict()


@router.get("/wallets/discovery/audit")
def get_latest_discovery_audit(session: Session = Depends(get_session)) -> dict:
    service = DiscoveryAuditService(session)
    latest = service.latest_report()
    if latest is None:
        return {
            "conclusion": "DISCOVERY_INSUFFICIENT_DATA",
            "rationale": "No discovery audit run yet. POST /wallets/discovery/audit to produce one.",
            "csv_path": str(service.csv_path),
            "json_path": str(service.json_path),
        }
    return latest


@router.get("/wallets/discovery/export")
def discovery_export_paths(session: Session = Depends(get_session)) -> dict:
    return DiscoveryAuditService(session).export_paths()


@router.get("/wallets/winrate/summary")
def winrate_summary(session: Session = Depends(get_session)) -> dict:
    service = DiscoveryAuditService(session)
    latest = service.latest_report()
    if latest is None:
        return {
            "available": False,
            "message": "Run POST /wallets/discovery/audit first.",
        }
    wallets = latest.get("audited_wallets", []) or []
    rates = [w.get("resolved_market_win_rate") for w in wallets if isinstance(w.get("resolved_market_win_rate"), (int, float))]
    confidences: dict[str, int] = {}
    for w in wallets:
        confidences[w.get("win_rate_confidence", "INSUFFICIENT_DATA")] = confidences.get(w.get("win_rate_confidence", "INSUFFICIENT_DATA"), 0) + 1
    avg = round(sum(rates) / len(rates), 4) if rates else None
    return {
        "available": True,
        "audited_wallets_count": latest.get("audited_wallets_count", 0),
        "average_win_rate": latest.get("average_win_rate", avg),
        "median_win_rate": latest.get("median_win_rate"),
        "wallets_with_reliable_win_rate_count": latest.get("wallets_with_reliable_win_rate_count", 0),
        "wallets_with_insufficient_win_rate_count": latest.get("wallets_with_insufficient_win_rate_count", 0),
        "win_rate_confidence_breakdown": confidences,
        "average_resolved_market_sample_size": latest.get("average_resolved_market_sample_size", 0),
        "data_source": latest.get("data_source"),
        "conclusion": latest.get("conclusion"),
    }


@router.get("/wallets/winrate/top")
def winrate_top(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    service = DiscoveryAuditService(session)
    latest = service.latest_report()
    wallets = (latest or {}).get("audited_wallets", []) or []
    eligible = [
        w
        for w in wallets
        if isinstance(w.get("resolved_market_win_rate"), (int, float))
        and w.get("win_rate_confidence") in {"MEDIUM", "HIGH"}
    ]
    eligible.sort(key=lambda w: (w["resolved_market_win_rate"], w.get("market_sample_size", 0)), reverse=True)
    return eligible[:limit]


# ---------------- v0.5 market-first wallet views ----------------


@router.get("/wallets/market-first/top")
def market_first_top(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    return MarketFirstDiscoveryService(session).list_top_wallets(limit=limit)


# ---------------- v0.7.8 P6 — 12-tier cohort with WR buckets ----------------


@router.get("/wallets/cohort")
def get_cohort_p6(limit: int = 200, session: Session = Depends(get_session)) -> dict:
    """Active cohort filtered + ranked per current capital tier.

    Returns:
      - current_tier (NANO/TINY/.../INST)
      - current_capital (BotState.paper_capital)
      - allowed_elite_buckets / allowed_strong_buckets (per tier rule)
      - counts: total_elite, total_strong, by_bucket {GOLD/SILVER/BRONZE}
      - tradable_now: count of wallets currently allowed to trade
      - wallets[]: top N rows sorted by priority, with bucket + tradable flag
    """
    from sqlmodel import select
    from app.models.bot import BotState
    from app.models.wallet import MarketFirstWalletRecord
    from app.services.capital_allocator import (
        _resolve_tier,
        classify_wr_bucket,
        is_wallet_allowed_at_tier,
    )

    # 2026-05-09: use effective capital (= paper_capital + session-PnL) so the
    # /wallets/cohort endpoint reflects the auto-ramping tier in real time.
    from app.services.paper_trading_engine import compute_effective_paper_capital
    capital = compute_effective_paper_capital(session)
    rule = _resolve_tier(capital)

    rows = session.exec(
        select(MarketFirstWalletRecord).where(
            MarketFirstWalletRecord.candidate_status.in_(["ELITE", "STRONG"])
        )
    ).all()

    counts = {
        "total_elite": 0,
        "total_strong": 0,
        "elite_by_bucket": {"GOLD": 0, "SILVER": 0, "BRONZE": 0, "REGULAR": 0},
        "strong_by_bucket": {"GOLD": 0, "SILVER": 0, "BRONZE": 0, "REGULAR": 0},
    }
    enriched = []
    tradable_now = 0
    for r in rows:
        bucket = classify_wr_bucket(r.resolved_market_win_rate)
        tradable = is_wallet_allowed_at_tier(
            r.candidate_status, r.resolved_market_win_rate, capital
        )
        if r.candidate_status == "ELITE":
            counts["total_elite"] += 1
            if bucket in counts["elite_by_bucket"]:
                counts["elite_by_bucket"][bucket] += 1
        elif r.candidate_status == "STRONG":
            counts["total_strong"] += 1
            if bucket in counts["strong_by_bucket"]:
                counts["strong_by_bucket"][bucket] += 1
        if tradable:
            tradable_now += 1
        enriched.append({
            "address": r.address,
            "candidate_status": r.candidate_status,
            "win_rate": r.resolved_market_win_rate,
            "wr_bucket": bucket,
            "resolved_winning": r.resolved_winning_markets,
            "resolved_losing": r.resolved_losing_markets,
            "sample_wl": (r.resolved_winning_markets or 0) + (r.resolved_losing_markets or 0),
            "recent_activity_score": r.recent_activity_score,
            "composite_score": r.composite_score,
            "best_category": r.best_category,
            "tradable_now": tradable,
        })

    # Sort: tradable_now first, then by priority (ELITE GOLD active first)
    def _priority(w: dict) -> tuple:
        elite_bonus = 1000 if w["candidate_status"] == "ELITE" else 0
        wr = w["win_rate"] or 0.0
        bucket_bonus = 500 if wr >= 0.99 else 300 if wr >= 0.95 else 100 if wr >= 0.90 else 0
        act = w["recent_activity_score"] or 0
        stale = -100 if act < 25 else 0
        return (
            int(w["tradable_now"]),
            elite_bonus + bucket_bonus + act + stale,
            wr,
            w["sample_wl"],
        )

    enriched.sort(key=_priority, reverse=True)

    return {
        "current_tier": rule["name"],
        "current_capital": capital,
        "allowed_elite_buckets": sorted(rule["allowed_elite_buckets"]),
        "allowed_strong_buckets": sorted(rule["allowed_strong_buckets"]),
        "max_open_positions": rule["max_open_positions_cap"],
        "max_total_exposure": rule["max_total_exposure_cap"],
        "counts": counts,
        "tradable_now": tradable_now,
        "wallets": enriched[:limit],
    }


@router.get("/wallets/market-first/{address}")
def market_first_wallet(address: str, session: Session = Depends(get_session)) -> dict:
    record = MarketFirstDiscoveryService(session).get_wallet(address)
    if record is None:
        raise HTTPException(status_code=404, detail="Wallet not yet covered by a market-first discovery run")
    return record


@router.get("/wallets/{address}")
def get_wallet(address: str, session: Session = Depends(get_session)) -> dict:
    auditor = SmartWalletAuditor(session)
    record = auditor.get_wallet(address)
    if record is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return record


@router.get("/wallets/{address}/audit")
def get_wallet_audit(address: str, session: Session = Depends(get_session)) -> dict:
    audit = SmartWalletAuditor(session).get_latest_audit(address)
    if audit is None:
        raise HTTPException(status_code=404, detail="No audit yet for wallet")
    return {
        "address": audit.address,
        "smart_score": audit.smart_score,
        "whale_score": audit.whale_score,
        "reliability": audit.reliability,
        "copyability": audit.copyability,
        "confidence": audit.confidence,
        "tier": audit.tier,
        "specialty": audit.specialty,
        "pnl": audit.pnl,
        "roi": audit.roi,
        "win_rate": audit.win_rate,
        "sample_size": audit.sample_size,
        "suspicious": audit.suspicious,
        "suspicious_reason": audit.suspicious_reason,
        "data_source": audit.data_source,
        "audit_at": audit.audit_at.isoformat() if audit.audit_at else None,
    }


@router.get("/wallets/{address}/trades")
def get_wallet_trades(address: str, limit: int = 200, session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": trade.id,
            "market_id": trade.market_id,
            "outcome": trade.outcome,
            "side": trade.side,
            "price": trade.price,
            "size": trade.size,
            "notional_usd": trade.notional_usd,
            "traded_at": trade.traded_at.isoformat() if trade.traded_at else None,
            "data_source": trade.data_source,
        }
        for trade in SmartWalletAuditor(session).get_wallet_trades(address, limit=limit)
    ]


@router.get("/wallets/{address}/winrate")
def get_wallet_winrate(address: str, session: Session = Depends(get_session)) -> dict:
    return WinRateEngine(session).compute_wallet_win_rate(address).to_dict()


@router.get("/wallets/{address}/category-winrate")
def get_wallet_category_winrate(address: str, session: Session = Depends(get_session)) -> dict:
    return MarketFirstDiscoveryService(session).category_winrate(address)


@router.post("/wallets/audit/run")
def run_wallet_audit(payload: WalletAuditRequest, session: Session = Depends(get_session)) -> dict:
    return SmartWalletAuditor(session).audit_wallet(payload.address).to_dict()


@router.post("/wallets/audit/run-batch")
def run_wallet_audit_batch(payload: WalletAuditBatchRequest, session: Session = Depends(get_session)) -> dict:
    auditor = SmartWalletAuditor(session)
    results = auditor.audit_wallet_batch(addresses=payload.addresses, limit=payload.limit)
    return {
        "audited": len(results),
        "results": [result.to_dict() for result in results],
    }


@router.get("/wallets/audit/report")
def wallet_audit_report(session: Session = Depends(get_session)) -> dict:
    return SmartWalletAuditor(session).export_wallet_audit_report()
