"""Tests for the v0.5.4 ValidatedPaperUniverse merge logic.

The universe is the single source of truth for "wallets the bot is allowed to
copy". It must:

- include only ELITE/STRONG with sample gates (>=100 ELITE, >=30 STRONG, validation samples >= 10/5)
- exclude OUTLIER_FLAGGED, BIASED_SAMPLE, FAILED_VALIDATION, CANDIDATE_ELITE
- carry per-row ``allowed_safe / allowed_aggressive / allowed_full_paper`` flags
- collapse the same wallet seen in multiple runs into a single row carrying
  the combined ``discovery_run_source`` ("730d+1095d") and the strongest tier
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.validated_paper_universe import ValidatedPaperUniverse


def _write_validation_report(path: Path, rows: list[dict]) -> None:
    payload = {"rows": rows}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _row(
    address: str,
    *,
    candidate_status: str,
    expanded_sample: int,
    expanded_win_rate: float,
    discovery_win_rate: float | None,
    validation_sample: int,
    validation_win_rate: float | None,
    warnings: list[str] | None = None,
) -> dict:
    return {
        "address": address,
        "candidate_status": candidate_status,
        "expanded_sample": expanded_sample,
        "expanded_win_rate": expanded_win_rate,
        "discovery_win_rate": discovery_win_rate,
        "validation_sample": validation_sample,
        "validation_win_rate": validation_win_rate,
        "warnings": warnings or [],
    }


def test_universe_excludes_outlier_biased_failed_candidate(tmp_path, monkeypatch) -> None:
    pass  # exports_dir patched per-instance below

    src = tmp_path / "val.json"
    _write_validation_report(
        src,
        [
            _row("0xelite", candidate_status="ELITE", expanded_sample=150, expanded_win_rate=0.92, discovery_win_rate=0.93, validation_sample=40, validation_win_rate=0.91),
            _row("0xstrong", candidate_status="STRONG", expanded_sample=45, expanded_win_rate=0.78, discovery_win_rate=0.80, validation_sample=15, validation_win_rate=0.75),
            _row("0xout", candidate_status="OUTLIER_FLAGGED", expanded_sample=120, expanded_win_rate=0.83, discovery_win_rate=0.88, validation_sample=23, validation_win_rate=0.65),
            _row("0xbias", candidate_status="BIASED_SAMPLE", expanded_sample=120, expanded_win_rate=0.95, discovery_win_rate=0.95, validation_sample=20, validation_win_rate=0.93),
            _row("0xfail", candidate_status="FAILED_VALIDATION", expanded_sample=80, expanded_win_rate=0.55, discovery_win_rate=0.85, validation_sample=20, validation_win_rate=0.30),
            _row("0xcand", candidate_status="CANDIDATE_ELITE", expanded_sample=22, expanded_win_rate=1.0, discovery_win_rate=1.0, validation_sample=8, validation_win_rate=1.0),
            _row("0xthin", candidate_status="ELITE", expanded_sample=130, expanded_win_rate=0.83, discovery_win_rate=0.84, validation_sample=4, validation_win_rate=0.80),
        ],
    )

    universe = ValidatedPaperUniverse()
    universe.exports_dir = tmp_path
    summary = universe.merge([("test", src)])

    assert summary.total_entries == 2
    assert summary.elite_count == 1
    assert summary.strong_count == 1
    assert summary.excluded_outlier_count == 1
    assert summary.excluded_biased_count == 1
    assert summary.excluded_failed_count == 1
    # 0xcand (CANDIDATE_ELITE) + 0xthin (ELITE but validation_sample<10) → excluded as candidate.
    assert summary.excluded_candidate_count == 2

    csv_path = Path(summary.csv_path)
    assert csv_path.exists()
    csv_rows = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(csv_rows) == 3  # header + 2 entries


def test_universe_allowed_flags_per_tier_and_gap(tmp_path, monkeypatch) -> None:
    pass  # exports_dir patched per-instance below

    src = tmp_path / "val.json"
    _write_validation_report(
        src,
        [
            # ELITE with gap 0.05 and val_wr 0.85 → all flags on.
            _row("0xclean_elite", candidate_status="ELITE", expanded_sample=150, expanded_win_rate=0.92, discovery_win_rate=0.92, validation_sample=40, validation_win_rate=0.87),
            # ELITE with gap 0.12 (> SAFE 0.10) → safe OFF, aggressive/full ON.
            _row("0xwide_gap", candidate_status="ELITE", expanded_sample=150, expanded_win_rate=0.92, discovery_win_rate=0.92, validation_sample=40, validation_win_rate=0.80),
            # STRONG → safe OFF, aggressive/full ON.
            _row("0xstrong", candidate_status="STRONG", expanded_sample=45, expanded_win_rate=0.78, discovery_win_rate=0.80, validation_sample=15, validation_win_rate=0.75),
        ],
    )
    universe = ValidatedPaperUniverse()
    universe.exports_dir = tmp_path
    summary = universe.merge([("test", src)])
    json_path = Path(summary.json_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    by_addr = {row["address"]: row for row in payload["rows"]}
    assert by_addr["0xclean_elite"]["allowed_safe"] is True
    assert by_addr["0xclean_elite"]["allowed_aggressive"] is True
    assert by_addr["0xclean_elite"]["allowed_full_paper"] is True
    assert by_addr["0xwide_gap"]["allowed_safe"] is False
    assert by_addr["0xwide_gap"]["allowed_aggressive"] is True
    assert by_addr["0xstrong"]["allowed_safe"] is False
    assert by_addr["0xstrong"]["allowed_aggressive"] is True
    assert by_addr["0xstrong"]["allowed_full_paper"] is True


def test_universe_demotes_legacy_elite_failing_strict_gap(tmp_path) -> None:
    """A legacy validation report (predating the OUTLIER_FLAGGED status) may
    still tag a wallet as ELITE even though its discovery↔validation gap is
    23% or its validation WR is 65%. The merge must re-enforce the strict
    gates and excluded such wallets — otherwise they leak into AGGRESSIVE /
    FULL_PAPER as 'allowed_aggressive=True'."""
    src = tmp_path / "legacy_val.json"
    _write_validation_report(
        src,
        [
            # Same shape that bit us in production: discovery 88% on 84 markets,
            # validation 65% on 23 markets → gap 23%. Legacy run tagged ELITE.
            _row(
                "0xa66790e2",
                candidate_status="ELITE",
                expanded_sample=107,
                expanded_win_rate=0.83,
                discovery_win_rate=0.88,
                validation_sample=23,
                validation_win_rate=0.65,
            ),
            # Sanity: a clean ELITE with tight gap survives.
            _row(
                "0xclean",
                candidate_status="ELITE",
                expanded_sample=200,
                expanded_win_rate=0.96,
                discovery_win_rate=0.96,
                validation_sample=40,
                validation_win_rate=0.94,
            ),
        ],
    )
    universe = ValidatedPaperUniverse()
    universe.exports_dir = tmp_path
    summary = universe.merge([("legacy", src)])
    payload = json.loads(Path(summary.json_path).read_text(encoding="utf-8"))
    addrs = {row["address"] for row in payload["rows"]}
    assert "0xa66790e2" not in addrs, "legacy ELITE failing strict gates must be excluded"
    assert "0xclean" in addrs
    assert summary.excluded_outlier_count >= 1


def test_universe_merges_same_wallet_across_runs(tmp_path, monkeypatch) -> None:
    pass  # exports_dir patched per-instance below

    src_a = tmp_path / "val_730d.json"
    src_b = tmp_path / "val_1095d.json"
    _write_validation_report(
        src_a,
        [
            _row("0xshared", candidate_status="STRONG", expanded_sample=66, expanded_win_rate=0.89, discovery_win_rate=0.89, validation_sample=31, validation_win_rate=0.90),
        ],
    )
    _write_validation_report(
        src_b,
        [
            # Same wallet, larger sample, ELITE in the bigger run.
            _row("0xshared", candidate_status="ELITE", expanded_sample=200, expanded_win_rate=0.88, discovery_win_rate=0.89, validation_sample=60, validation_win_rate=0.87),
        ],
    )
    universe = ValidatedPaperUniverse()
    universe.exports_dir = tmp_path
    summary = universe.merge([("730d", src_a), ("1095d", src_b)])
    json_path = Path(summary.json_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    rows = payload["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["tier"] == "ELITE", "merge must keep the strongest tier across runs"
    assert "730d" in row["discovery_run_source"] and "1095d" in row["discovery_run_source"]
