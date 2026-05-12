"""P0.5 (Round 8, 2026-05-12) — BASE_LIVE max_total_exposure = 0.95.

Verifies the BASE_LIVE preset exposes 0.95 so the CAPITAL_TIER_RULES ladder
(75% NANO → 95% MEDIUM+) takes effect via `min(base, tier_cap) = tier_cap`
instead of being silently capped at 0.70.
"""

from __future__ import annotations

import pytest

from app.services.preset_loader import get_preset, reload_presets


@pytest.fixture(autouse=True)
def fresh_presets():
    """Force fresh load each test (in case other tests cached older presets)."""
    reload_presets()
    yield
    reload_presets()


def test_base_live_exposure_aligned_with_tier_ladder():
    """BASE_LIVE.max_total_exposure must be ≥ the maximum tier_cap (0.95)
    so the ladder is not silently capped."""
    preset = get_preset("BASE_LIVE")
    assert preset is not None
    assert preset.max_total_exposure >= 0.95, (
        f"BASE_LIVE.max_total_exposure = {preset.max_total_exposure} would "
        f"cap the tier ladder (75-95%) via min(base, tier_cap). Expected ≥0.95 "
        f"per P0.5 Round 8 fix."
    )


def test_base_paper_not_more_permissive_than_base_live():
    """Paper ≤ live strict invariant — BASE_PAPER must not exceed BASE_LIVE."""
    paper = get_preset("BASE_PAPER")
    live = get_preset("BASE_LIVE")
    assert paper is not None and live is not None
    assert paper.max_total_exposure <= live.max_total_exposure, (
        f"Paper-≤-live violated: BASE_PAPER={paper.max_total_exposure} > "
        f"BASE_LIVE={live.max_total_exposure}"
    )


def test_base_live_other_fields_intact():
    """Sanity: P0.5 must only change max_total_exposure, not other fields."""
    preset = get_preset("BASE_LIVE")
    assert preset is not None
    assert preset.max_open_positions == 100
    assert preset.reserve_buffer == 0.20
    assert preset.risk_per_trade == 0.01
    assert preset.min_stake == 5.0
    assert "ELITE" in preset.allowed_tiers
    assert preset.min_ev_lb == 0.05
