from app.services.risk_engine import RiskEngine


def test_risk_rejects_low_signal_score() -> None:
    decision = RiskEngine().validate_trade(signal_score=20, spread=0.01, liquidity=10_000, capital=10_000)

    assert decision.approved is False
    assert decision.reason == "Signal score below minimum"


def test_risk_accepts_clean_trade() -> None:
    """v0.7.2: position_size depends on Settings.max_risk_per_trade (env-driven).
    The test asserts the legacy validate_trade path approves the signal and
    sizes proportionally to capital (capital * max_risk_per_trade).
    """
    from app.config import get_settings

    decision = RiskEngine().validate_trade(signal_score=80, spread=0.01, liquidity=10_000, capital=10_000)

    assert decision.approved is True
    expected = round(10_000 * get_settings().max_risk_per_trade, 2)
    assert decision.position_size == expected
