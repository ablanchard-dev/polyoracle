from datetime import UTC, datetime, timedelta

from app.services.copyable_edge_engine import CopyableEdgeEngine


def test_copyable_edge_strong_when_metrics_clean() -> None:
    engine = CopyableEdgeEngine()
    breakdown = engine.compute_copyable_edge(
        wallet_score=85,
        signal_score=85,
        spread_pct=0.005,
        slippage=0.002,
        copy_delay_seconds=30,
        original_price=0.50,
        current_price=0.50,
        side="BUY",
    )
    assert breakdown.copyable_edge > 0
    assert breakdown.classification in {"STRONG_EDGE", "WEAK_EDGE"}


def test_copyable_edge_negative_when_late() -> None:
    engine = CopyableEdgeEngine()
    breakdown = engine.compute_copyable_edge(
        wallet_score=80,
        signal_score=80,
        spread_pct=0.04,
        slippage=0.05,
        copy_delay_seconds=3_600,
        original_price=0.50,
        current_price=0.60,
        side="BUY",
    )
    assert breakdown.classification in {"NEGATIVE_EDGE", "NO_EDGE"}


def test_copy_delay_compute_respects_naive_datetime() -> None:
    engine = CopyableEdgeEngine()
    naive = datetime.utcnow() - timedelta(seconds=120)
    delay = engine.compute_copy_delay(naive)
    assert delay >= 0
