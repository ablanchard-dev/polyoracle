"""Load CapitalAllocator presets from ``app/config/presets.yaml``.

Single source of truth for preset values. Importing this module gives
typed ``AllocatorParams`` instances ready to plug into
``CapitalAllocator.evaluate_trade``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.services.capital_allocator import AllocatorParams


PRESETS_PATH = Path(__file__).resolve().parent.parent / "config" / "presets.yaml"


def _to_params(name: str, data: dict[str, Any]) -> AllocatorParams:
    return AllocatorParams(
        name=name,
        max_total_exposure=float(data["max_total_exposure"]),
        max_wallet_exposure=float(data["max_wallet_exposure"]),
        max_market_exposure=float(data["max_market_exposure"]),
        max_open_positions=data.get("max_open_positions"),
        reserve_buffer=float(data.get("reserve_buffer", 0.0)),
        risk_per_trade=float(data["risk_per_trade"]),
        min_stake=float(data["min_stake"]),
        allowed_tiers=frozenset(data.get("allowed_tiers", [])),
        min_ev_lb=float(data["min_ev_lb"]),
        min_copyable_edge=float(data["min_copyable_edge"]),
        max_spread_pct=float(data["max_spread_pct"]),
        min_liquidity=float(data["min_liquidity"]),
        min_liquidity_score=(
            float(data["min_liquidity_score"])
            if data.get("min_liquidity_score") is not None
            else None
        ),
        polymarket_fee_pct=float(data["polymarket_fee_pct"]),
        min_edge_after_fees=float(data["min_edge_after_fees"]),
        late_entry_thresholds={
            str(k): float(v) for k, v in (data.get("late_entry_thresholds") or {}).items()
        },
        late_entry_default=float(data.get("late_entry_default", 300)),
        require_orderbook_quality=bool(data.get("require_orderbook_quality", True)),
        live_allowed=bool(data.get("live_allowed", False)),
        description=str(data.get("description") or "").strip(),
        hard_safety_caps={
            str(k): float(v) for k, v in (data.get("hard_safety_caps") or {}).items()
        },
        capital_aware_thresholds=bool(data.get("capital_aware_thresholds", True)),
    )


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, dict[str, Any]]:
    with PRESETS_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"presets.yaml must be a mapping at the top level, got {type(data)}")
    return data


def list_preset_names() -> list[str]:
    return sorted(_load_raw().keys())


def get_preset(name: str) -> AllocatorParams:
    data = _load_raw()
    if name not in data:
        raise KeyError(f"unknown preset {name!r} (available: {list_preset_names()})")
    return _to_params(name, data[name])


def get_all_presets() -> dict[str, AllocatorParams]:
    return {n: _to_params(n, d) for n, d in _load_raw().items()}


def reload_presets() -> None:
    """Drop the LRU cache so callers will re-read the YAML on next access."""
    _load_raw.cache_clear()
