"""Phase G HOTFIX — auto-reclass daily signature (review audit 2026-05-13).

The main.py @startup task previously called `run_weekly_reclass(session,
dry_run=False)`. Service signature requires `db_path` + `backup_dir`. The
mismatch raised TypeError silently caught → cron ran into nothing.

This test verifies the call shape matches the service signature.
"""

from __future__ import annotations

import inspect

import pytest


def test_run_weekly_reclass_signature():
    """The service must expose run_weekly_reclass(session, *, db_path,
    backup_dir, dry_run). main.py must pass all required kwargs."""
    from app.services.weekly_reclass_service import run_weekly_reclass

    sig = inspect.signature(run_weekly_reclass)
    params = sig.parameters
    assert "session" in params, "First positional param must be session"
    assert "db_path" in params, "Service requires db_path kwarg"
    assert "backup_dir" in params, "Service requires backup_dir kwarg"
    assert "dry_run" in params, "Service supports dry_run kwarg"

    # db_path and backup_dir must be keyword-only
    assert params["db_path"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["backup_dir"].kind == inspect.Parameter.KEYWORD_ONLY


def test_main_py_uses_correct_signature():
    """Inspect main.py source: it must NOT call run_weekly_reclass with the
    old buggy `(session, dry_run=False)` form."""
    from pathlib import Path
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    src = main_path.read_text()
    # Find the call site
    assert "run_weekly_reclass(" in src, "main.py must import + call run_weekly_reclass"
    # The CORRECT call must include db_path= and backup_dir=
    # Locate the call line
    call_idx = src.index("run_weekly_reclass(")
    # Take 300 chars from call site
    call_block = src[call_idx:call_idx + 400]
    assert "db_path=" in call_block, (
        "main.py call to run_weekly_reclass missing db_path kwarg — "
        "TypeError silent at runtime. Cron would tournerait à vide."
    )
    assert "backup_dir=" in call_block, (
        "main.py call to run_weekly_reclass missing backup_dir kwarg."
    )


def test_main_py_imports_required_modules():
    """The reclass loop also needs Path + Session imports nearby."""
    from pathlib import Path
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    src = main_path.read_text()
    assert "from pathlib import Path" in src or "import pathlib" in src
    assert "from sqlmodel import Session" in src or "import sqlmodel" in src
