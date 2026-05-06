from sqlmodel import SQLModel, Session, create_engine

from app.services.smart_wallet_auditor import SmartWalletAuditor


def _engine() -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_smart_wallet_score_blends_dimensions() -> None:
    profile = {
        "address": "0xabc",
        "pnl": 120_000,
        "roi": 0.30,
        "win_rate": 0.60,
        "market_count": 80,
        "volume": 800_000,
        "data_source": "mock",
    }
    engine = _engine()
    with Session(engine) as session:
        auditor = SmartWalletAuditor(session)
        breakdown = auditor.compute_smart_wallet_score(profile, trades=[])
    assert breakdown.smart_score > 0
    assert breakdown.smart_score <= 100
    assert breakdown.pnl_score > 0


def test_audit_wallet_persists_audit_and_classifies_tier() -> None:
    engine = _engine()
    with Session(engine) as session:
        auditor = SmartWalletAuditor(session)
        result = auditor.audit_wallet("0x7a91...c42f")
        assert result.tier in {"ELITE", "STRONG", "WATCH", "WEAK", "IGNORE", "SUSPICIOUS", "INSUFFICIENT_DATA"}
        report = auditor.export_wallet_audit_report()
    assert report["wallets"], "audit must persist at least one wallet record"


def test_detect_suspicious_flags_unbalanced_profile() -> None:
    engine = _engine()
    with Session(engine) as session:
        auditor = SmartWalletAuditor(session)
        suspicious, reason = auditor.detect_suspicious_wallet(
            {"pnl": 50_000, "win_rate": 0.95, "market_count": 4, "volume": 10_000},
            trades=[],
        )
    assert suspicious is True
    assert reason
