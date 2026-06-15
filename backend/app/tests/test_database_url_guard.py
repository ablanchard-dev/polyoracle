"""v0.7.5 — DATABASE_URL startup guard tests.

Locks in the contract from the v0.7.4 incident: the backend must NEVER
silently boot with a wrong DB. Three independent checks must hold:

1. Forbidden-pattern check  (env or resolved path contains 'lumenia',
   '/Downloads/', '/Temp/', etc.)
2. Connection sanity check  (DB file is reachable)
3. MFWR row count check     (>= 100, the truth pool sanity floor)

Any failure -> RuntimeError with an actionable message.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine


@contextmanager
def _patched_database_module(database_url: str, resolved_path: str):
    """Patch the app.database module-level engine + URL, plus override
    the settings.resolved_sqlite_path read-only property via patch.object.
    Runs the guard against the patched state."""
    import app.database as db_mod
    import app.config as cfg_mod

    original_engine = db_mod.engine
    original_url = db_mod.database_url

    test_engine = create_engine(database_url)
    db_mod.engine = test_engine
    db_mod.database_url = database_url

    # resolved_sqlite_path is a Pydantic property — patch the property itself
    # at the class level so all settings.* accesses return our test path.
    with patch.object(
        type(cfg_mod.get_settings()),
        "resolved_sqlite_path",
        new_callable=lambda: property(lambda self: Path(resolved_path)),
    ):
        try:
            yield test_engine
        finally:
            db_mod.engine = original_engine
            db_mod.database_url = original_url


def _seed_mfwr_rows(engine, n: int) -> None:
    """Create the marketfirstwalletrecord table and insert N dummy rows."""
    from app.models.wallet import MarketFirstWalletRecord  # noqa
    SQLModel.metadata.create_all(engine)
    with Session(engine) as sess:
        for i in range(n):
            sess.add(MarketFirstWalletRecord(
                address=f"0x{i:040x}",
                market_first_score=0.0,
                composite_score=0.0,
                tier="UNKNOWN",
                status="OK",
                win_rate_confidence="LOW",
                resolved_markets_traded=0,
                resolved_winning_markets=0,
                resolved_losing_markets=0,
                recent_activity_score=0.0,
                copyability_score=0.0,
                total_resolved_notional=0.0,
                average_position_size=0.0,
                median_position_size=0.0,
                data_source="test_seed",
            ))
        sess.commit()


# ============ 1. Forbidden URL patterns ============


def test_startup_refuses_lumenia_url():
    """An env DATABASE_URL containing 'lumenia' must trigger RuntimeError."""
    with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///C:/other-project/data/lumenia.db"}):
        from app.database import _validate_database_url
        with pytest.raises(RuntimeError, match=r"FATAL DATABASE_URL leak.*lumenia"):
            _validate_database_url()


def test_startup_refuses_downloads_path():
    """An env DATABASE_URL with '/Downloads/' (case-insensitive) must fail."""
    with patch.dict(os.environ, {"DATABASE_URL": "sqlite:////var/data/Downloads/anyproject/data.db"}):
        from app.database import _validate_database_url
        with pytest.raises(RuntimeError, match=r"FATAL DATABASE_URL leak.*[Dd]ownloads"):
            _validate_database_url()


def test_startup_refuses_temp_path():
    """An env DATABASE_URL with '/Temp/' must fail."""
    with patch.dict(os.environ, {"DATABASE_URL": "sqlite:////var/Temp/scratch.db"}):
        from app.database import _validate_database_url
        with pytest.raises(RuntimeError, match=r"FATAL DATABASE_URL leak.*[Tt]emp"):
            _validate_database_url()


# ============ 2. MFWR row count sanity ============


def test_startup_refuses_db_without_mfwr():
    """An in-memory SQLite (table exists but 0 MFWR rows) must trigger the
    sanity check failure (count < min). We use sqlite:// (in-memory) so the
    URL contains no path-based forbidden patterns."""
    import app.database as db_mod
    import app.config as cfg_mod

    original_engine = db_mod.engine
    original_url = db_mod.database_url
    test_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(test_engine)

    db_mod.engine = test_engine
    db_mod.database_url = "sqlite://"
    # patch resolved_sqlite_path to a clean polyoracle-like path (no temp)
    fake_path = Path("data/polyoracle.db")
    with patch.object(
        type(cfg_mod.get_settings()),
        "resolved_sqlite_path",
        new_callable=lambda: property(lambda self: fake_path),
    ):
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DATABASE_URL", None)
                from app.database import _validate_database_url
                with pytest.raises(RuntimeError, match=r"FATAL DB sanity check.*marketfirstwalletrecord"):
                    _validate_database_url()
        finally:
            db_mod.engine = original_engine
            db_mod.database_url = original_url


def test_startup_accepts_valid_polyoracle_db():
    """A SQLite with >= 100 MFWR rows under a clean path must validate."""
    import app.database as db_mod
    import app.config as cfg_mod

    original_engine = db_mod.engine
    original_url = db_mod.database_url
    test_engine = create_engine("sqlite://")
    _seed_mfwr_rows(test_engine, 150)

    db_mod.engine = test_engine
    db_mod.database_url = "sqlite://"
    fake_path = Path("data/polyoracle.db")
    with patch.object(
        type(cfg_mod.get_settings()),
        "resolved_sqlite_path",
        new_callable=lambda: property(lambda self: fake_path),
    ):
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DATABASE_URL", None)
                from app.database import _validate_database_url
                _validate_database_url()  # must not raise
        finally:
            db_mod.engine = original_engine
            db_mod.database_url = original_url


# ============ 3. Error message clarity ============


def test_error_message_helps_debugging():
    """The error message must mention DATABASE_URL, the offending pattern,
    AND tell the operator how to fix it."""
    with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///C:/Downloads/lumenia.db"}):
        from app.database import _validate_database_url
        with pytest.raises(RuntimeError) as exc_info:
            _validate_database_url()
        msg = str(exc_info.value)
        # Must contain the URL so the operator sees what was loaded
        assert "DATABASE_URL" in msg
        # Must contain a hint about how to fix
        assert "polyoracle" in msg.lower()
        # Must be classified as FATAL
        assert "FATAL" in msg


# ============ Bonus: real polyoracle DB validates (smoke test) ============


def test_real_polyoracle_db_passes_guard():
    """The actual production DB must pass the guard. Acts as a canary
    if the project layout drifts."""
    project_root = Path(__file__).resolve().parents[3]
    real_db = project_root / "data" / "polyoracle.db"
    if not real_db.exists():
        pytest.skip("real polyoracle.db not present (CI env?)")

    # Reset env to be safe
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATABASE_URL", None)
        # Reload settings/engine module-level state by re-importing
        # via _patched_database_module with the real path.
        with _patched_database_module(f"sqlite:///{real_db}", str(real_db)):
            from app.database import _validate_database_url
            _validate_database_url()  # must not raise
