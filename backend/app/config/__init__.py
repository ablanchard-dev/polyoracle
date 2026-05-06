"""Config package facade.

The repo historically has both ``app/config.py`` for runtime settings and
``app/config/`` for static artefacts such as ``presets.yaml``. Python resolves
``app.config`` to this package first, so re-export the runtime settings here to
keep ``from app.config import get_settings`` stable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config.py"
_SPEC = importlib.util.spec_from_file_location("_polyoracle_runtime_config", _SETTINGS_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import bootstrap guard
    raise ImportError(f"Cannot load runtime config from {_SETTINGS_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

Settings = _MODULE.Settings
BotMode = _MODULE.BotMode
get_settings = _MODULE.get_settings

__all__ = ["Settings", "BotMode", "get_settings"]
