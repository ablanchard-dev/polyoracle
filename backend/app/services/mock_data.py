from datetime import UTC, datetime, timedelta

from app.models.market import Market
from app.models.signal import Signal, SignalType
from app.models.wallet import Wallet


def mock_markets() -> list[Market]:
    now = datetime.now(UTC)
    return [
        Market(
            id="mock-election-001",
            question="Will Candidate A win the 2028 popular vote?",
            category="Politics",
            yes_price=0.54,
            no_price=0.46,
            volume_24h=185_000,
            volume_7d=1_420_000,
            spread=0.012,
            liquidity=240_000,
            open_interest=920_000,
            deadline=now + timedelta(days=280),
            opportunity_score=91.5,
        ),
        Market(
            id="mock-fed-002",
            question="Will the Fed cut rates by June?",
            category="Macro",
            yes_price=0.37,
            no_price=0.63,
            volume_24h=78_500,
            volume_7d=510_000,
            spread=0.021,
            liquidity=96_000,
            open_interest=410_000,
            deadline=now + timedelta(days=54),
            opportunity_score=82.2,
        ),
        Market(
            id="mock-crypto-003",
            question="Will ETH close above $5,000 this quarter?",
            category="Crypto",
            yes_price=0.29,
            no_price=0.71,
            volume_24h=42_800,
            volume_7d=305_000,
            spread=0.034,
            liquidity=61_000,
            open_interest=210_000,
            deadline=now + timedelta(days=67),
            opportunity_score=73.8,
        ),
    ]


def mock_wallets() -> list[Wallet]:
    now = datetime.now(UTC)
    return [
        Wallet(address="0x7a91...c42f", score=88.4, pnl=126_400, roi=0.34, win_rate=0.61, market_count=148, volume=1_850_000, specialty="Politics", last_trade_at=now),
        Wallet(address="0x54bd...91aa", score=81.7, pnl=74_200, roi=0.27, win_rate=0.58, market_count=96, volume=980_000, specialty="Macro", last_trade_at=now - timedelta(hours=2)),
        Wallet(address="0x13fe...78d0", score=76.3, pnl=42_900, roi=0.22, win_rate=0.64, market_count=52, volume=420_000, specialty="Crypto", last_trade_at=now - timedelta(hours=5)),
    ]


def mock_signals() -> list[Signal]:
    return [
        Signal(id="sig-001", market_id="mock-election-001", outcome="YES", signal_type=SignalType.SMART_CONSENSUS, score=86.0, confidence=0.78, reason="3 high-score wallets accumulated within 45 minutes while spread stayed tight.", action="paper", status="watch"),
        Signal(id="sig-002", market_id="mock-fed-002", outcome="NO", signal_type=SignalType.VOLUME_BREAKOUT, score=74.0, confidence=0.66, reason="24h volume is 2.4x recent baseline with improving orderbook depth.", action="watch", status="watch"),
        Signal(id="sig-003", market_id="mock-crypto-003", outcome="YES", signal_type=SignalType.LATE_CROWD_TRAP, score=62.0, confidence=0.59, reason="Retail-sized entries arrived after a fast price move; risk engine recommends no chase.", action="avoid", status="rejected"),
    ]
