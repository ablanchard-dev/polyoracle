from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.models.trade import PaperTrade
from app.models.wallet import MarketFirstWalletRecord
from app.services.paper_trading_engine import PaperTradingEngine

router = APIRouter(prefix="/paper", tags=["paper"])


class CloseRequest(BaseModel):
    exit_price: float
    reason: str | None = None


@router.get("/positions")
def get_paper_positions(session: Session = Depends(get_session)) -> list[dict]:
    return [trade.model_dump() for trade in PaperTradingEngine(session).update_paper_positions()]


@router.get("/trades")
def get_paper_trades(session: Session = Depends(get_session)) -> list[dict]:
    return PaperTradingEngine(session).export_trade_journal()


@router.post("/reset")
def reset_paper(session: Session = Depends(get_session)) -> dict[str, str]:
    PaperTradingEngine(session).reset()
    return {"status": "reset"}


@router.post("/close/{position_id}")
def close_position(position_id: str, payload: CloseRequest, session: Session = Depends(get_session)) -> dict:
    closed = PaperTradingEngine(session).close_position_by_id(position_id, payload.exit_price, reason=payload.reason)
    if closed is None:
        raise HTTPException(status_code=404, detail="Position not found or already closed")
    return closed.model_dump()


@router.get("/report")
def paper_report(session: Session = Depends(get_session)) -> dict:
    return PaperTradingEngine(session).generate_paper_report()


@router.get("/performance")
def paper_performance(session: Session = Depends(get_session)) -> dict:
    return PaperTradingEngine(session).performance()


@router.get("/efficiency")
def paper_copy_efficiency(session: Session = Depends(get_session)) -> dict:
    """Per-wallet copy efficiency: bot_wr / source_wallet_wr.

    Groups closed paper trades by wallet, computes bot win rate per wallet,
    compares to MFWR wallet win rate. Flags wallets with low copyability.
    """
    closed = session.exec(
        select(PaperTrade).where(
            PaperTrade.status == "closed",
            PaperTrade.auto == True,  # noqa: E712
        )
    ).all()

    if not closed:
        return {"wallets": [], "global_copy_efficiency": None, "total_closed": 0}

    # Group by wallet
    by_wallet: dict[str, list[PaperTrade]] = {}
    for t in closed:
        addr = t.wallet_address or "unknown"
        by_wallet.setdefault(addr, []).append(t)

    wallet_stats = []
    total_wins = 0
    total_trades = 0
    weighted_eff_sum = 0.0
    weighted_eff_count = 0

    for addr, trades in sorted(by_wallet.items(), key=lambda x: -len(x[1])):
        wins = sum(1 for t in trades if (t.realized_pnl or 0) > 0)
        n = len(trades)
        bot_wr = wins / n if n > 0 else 0.0
        total_wins += wins
        total_trades += n

        # Source wallet win rate from MFWR
        mfwr = session.get(MarketFirstWalletRecord, addr)
        source_wr = None
        candidate_status = None
        copy_eff = None
        if mfwr:
            candidate_status = mfwr.candidate_status
            w = mfwr.resolved_winning_markets or 0
            l = mfwr.resolved_losing_markets or 0
            if w + l > 0:
                source_wr = round(w / (w + l), 4)
                copy_eff = round(bot_wr / source_wr, 4) if source_wr > 0 else None
                if copy_eff is not None:
                    weighted_eff_sum += copy_eff * n
                    weighted_eff_count += n

        low_copyability = copy_eff is not None and copy_eff < 0.70 and n >= 10
        wallet_stats.append({
            "wallet": addr[:12] + "..." if len(addr) > 12 else addr,
            "wallet_full": addr,
            "candidate_status": candidate_status,
            "n_closed": n,
            "bot_wins": wins,
            "bot_wr": round(bot_wr, 4),
            "source_wr": source_wr,
            "copy_efficiency": copy_eff,
            "low_copyability": low_copyability,
        })

    global_bot_wr = total_wins / total_trades if total_trades > 0 else 0.0
    global_copy_eff = (
        round(weighted_eff_sum / weighted_eff_count, 4)
        if weighted_eff_count > 0 else None
    )

    return {
        "total_closed": total_trades,
        "total_wins": total_wins,
        "global_bot_wr": round(global_bot_wr, 4),
        "global_copy_efficiency": global_copy_eff,
        "low_copyability_count": sum(1 for w in wallet_stats if w["low_copyability"]),
        "wallets": wallet_stats,
    }
