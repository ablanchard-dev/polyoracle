"""v0.7.2 invariant test — keep the codebase free of named operational modes.

The bot has ONE runtime model: ``CapitalAllocator`` with capital-aware
sliding-scale tightening. The forbidden tokens below are *operational
mode/preset names* the user explicitly retired. They must not appear in
the production code paths.

A small whitelist tolerates:

* test files (regression coverage of the deprecation shim);
* the ``risk_mode.py`` deprecation shim itself;
* archive / replay / one-off scripts (``backend/_*.py``);
* docs/exports that reference the historical state for context.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


FORBIDDEN_TOKENS = (
    "SAFE_PAPER",
    "AGGRESSIVE_PAPER",
    "FULL_MIRROR_PAPER",
    "FULL_MIRROR_LIVE",
    "PRESET_SAFE",
    "PRESET_AGGRESSIVE",
    "PRESET_FULL_MIRROR",
    "PRESET_FULL_PAPER",
)

# Files that may still mention the legacy names. These are deprecation
# shims, regression tests, archive scripts, or output artefacts. Adding to
# this list = explicit acknowledgement that the file is *legacy* and not
# part of the v0.7.2+ runtime.
WHITELIST_PATH_FRAGMENTS = (
    # Tests (legacy regression coverage)
    "backend/app/tests/test_risk_modes.py",
    "backend/app/tests/test_risk_engine_integration_v0_5_8_1.py",
    "backend/app/tests/test_validated_paper_universe.py",
    "backend/app/tests/test_paper_auto_trading.py",
    "backend/app/tests/test_no_mode_names_in_codebase.py",  # this file lists tokens
    # Deprecation shim
    "backend/app/services/risk_mode.py",
    # Backtest projection (kept for historical comparison)
    "backend/app/services/edge_validation_engine.py",
    # Service comments that mention the legacy names in passing
    "backend/app/services/candidate_validation_service.py",
)

# Directories that are entirely off-limits to the scan.
EXCLUDED_DIR_FRAGMENTS = (
    "/data/backups/",
    "/data/exports/",
    "/.git/",
    "/.venv",
    "/node_modules/",
    "/__pycache__/",
    "/dist/",
    "/build/",
)


def _repo_root() -> Path:
    # tests dir is .../backend/app/tests, repo root is 3 levels up
    return Path(__file__).resolve().parents[3]


def _is_whitelisted(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    return any(rel.endswith(frag.replace("backend/", ""))
               or frag in rel
               or rel == frag
               for frag in WHITELIST_PATH_FRAGMENTS)


def _is_excluded(path: Path, root: Path) -> bool:
    rel = "/" + path.relative_to(root).as_posix()
    return any(frag in rel for frag in EXCLUDED_DIR_FRAGMENTS)


def _iter_scan_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for pattern in ("*.py", "*.yaml", "*.yml", "*.ts", "*.tsx", "*.json"):
        for path in root.rglob(pattern):
            if not path.is_file():
                continue
            if _is_excluded(path, root):
                continue
            candidates.append(path)
    return candidates


def test_no_forbidden_mode_names_in_runtime_code():
    """Grep all non-whitelisted files for forbidden mode/preset names.

    A failure here means a v0.7.2+ runtime file still references a named
    operational mode that should have been retired. Either rename the
    reference, or whitelist the file explicitly (after documenting why
    it's legacy).
    """
    root = _repo_root()
    pattern = re.compile("|".join(re.escape(tok) for tok in FORBIDDEN_TOKENS))
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _iter_scan_files(root):
        if _is_whitelisted(path, root):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not pattern.search(text):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                rel = path.relative_to(root).as_posix()
                offenders.setdefault(rel, []).append((line_no, line.strip()))
    if offenders:
        msg_lines = ["Forbidden mode names found in runtime code:"]
        for rel, hits in sorted(offenders.items()):
            msg_lines.append(f"  {rel}")
            for line_no, content in hits[:5]:
                msg_lines.append(f"    L{line_no}: {content[:160]}")
            if len(hits) > 5:
                msg_lines.append(f"    ... +{len(hits) - 5} more")
        msg_lines.append(
            "Either rename the references to v0.7.2 neutral names "
            "(BASE_PAPER / BASE_LIVE / capital_scenario_*) or add the path "
            "to WHITELIST_PATH_FRAGMENTS with a justification comment."
        )
        pytest.fail("\n".join(msg_lines))


def test_whitelist_paths_actually_exist():
    """Whitelist hygiene — every path we whitelist must exist on disk."""
    root = _repo_root()
    missing = []
    for frag in WHITELIST_PATH_FRAGMENTS:
        if "/" not in frag:
            continue
        candidate = root / frag
        if not candidate.exists():
            missing.append(frag)
    assert not missing, (
        "Whitelist references missing paths (were files renamed/deleted?): "
        + ", ".join(missing)
    )
