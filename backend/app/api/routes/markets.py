from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.database import get_session
from app.schemas.market import MarketRead
from app.services.market_scanner import MarketScanner

router = APIRouter(tags=["markets"])


@router.get("/markets", response_model=list[MarketRead])
def get_markets(session: Session = Depends(get_session)) -> list[MarketRead]:
    return MarketScanner(session).rank_markets()


@router.get("/markets/hot", response_model=list[MarketRead])
def get_hot_markets(session: Session = Depends(get_session)) -> list[MarketRead]:
    return MarketScanner(session).detect_hot_markets()


@router.get("/markets/tradable", response_model=list[MarketRead])
def get_tradable_markets(session: Session = Depends(get_session)) -> list[MarketRead]:
    scanner = MarketScanner(session)
    settings = scanner.settings
    return [
        market
        for market in scanner.rank_markets()
        if market.spread <= settings.max_spread and market.liquidity >= settings.min_liquidity
    ]


@router.get("/markets/{market_id}", response_model=MarketRead)
def get_market_detail(market_id: str, session: Session = Depends(get_session)) -> MarketRead:
    market = MarketScanner(session).fetch_market_details(market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")
    return market


@router.get("/markets/{market_id}/orderbook")
def get_market_orderbook(market_id: str, session: Session = Depends(get_session)) -> dict:
    return MarketScanner(session).fetch_first_market_orderbook(market_id)


@router.get("/orderbooks/{token_id}")
def get_orderbook_by_token(token_id: str, session: Session = Depends(get_session)) -> dict:
    return MarketScanner(session).fetch_orderbook(token_id)
