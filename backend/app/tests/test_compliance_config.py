from app.config import Settings
from app.services.compliance_config import ComplianceConfig


def test_compliance_blocks_live_by_default() -> None:
    compliance = ComplianceConfig(Settings(live_enabled=False))

    assert compliance.is_live_allowed() is False
    assert compliance.live_blocked_reason() == "LIVE_ENABLED=false"


def test_compliance_requires_allowed_jurisdiction() -> None:
    compliance = ComplianceConfig(
        Settings(live_enabled=True, compliance_jurisdiction="UNSET", allowed_live_jurisdictions=[])
    )

    assert compliance.is_live_allowed() is False
