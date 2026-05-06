from sqlmodel import Session, create_engine

from app.services.wallet_tracker import WalletTracker


def test_wallet_score_uses_multiple_dimensions() -> None:
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        tracker = WalletTracker(session)
        score = tracker.compute_wallet_score("0x7a91...c42f")

    assert score > 70
    assert score < 100
