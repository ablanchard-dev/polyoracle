"""Phase B baseline constants (POLYORACLE 2026-05-11).

Sépare le `strict_cutover_at` (le moment où PAPER_LIVE_STRICT a été activé)
du `EFFECTIVE_BASELINE_T0` (le moment où le bot a effectivement commencé
à ouvrir des trades en strict mode, après le fix B `UNKNOWN_CATEGORY`).

Pourquoi : entre 2026-05-11T01:54Z (strict_cutover_at) et 2026-05-11T13:21Z
(restart post-fix category_resolver), le bot a tourné en strict mode MAIS
n'a opened ZERO trade à cause du bug UNKNOWN_CATEGORY. Ces 11h27 ne
représentent pas la performance du bot — elles servent UNIQUEMENT au
diagnostic du bug structurel.

Les métriques Phase B (PF, WR, DD, cadence, growth) DOIVENT utiliser
EFFECTIVE_BASELINE_T0 comme référence, pas strict_cutover_at. Sinon les
chiffres sont dilués par les 11h27 inutiles.

Verbatim review Round 4 (2026-05-11 soir) :
> "redéfinir strict_effective_t0=2026-05-11T13:21Z — exclure les 11h cassées"
> "Le smoke initial 01:54 → 13:21 ne compte pas pour la performance. Il
>  compte seulement pour le diagnostic."
"""

from __future__ import annotations

from datetime import UTC, datetime

# Backend restart after category_resolver fix B. First trade after this : 13:21:40Z.
# We use the restart timestamp itself (slightly earlier) so any trade in the
# fix-restart window is included.
EFFECTIVE_BASELINE_T0 = datetime(2026, 5, 11, 13, 21, 33, tzinfo=UTC)
EFFECTIVE_BASELINE_T0_ISO = EFFECTIVE_BASELINE_T0.isoformat(timespec="seconds")

# Source: backend.dev.err.log shows old PID 15140 finished, new PID 29484
# started ~13:21Z, polling resumed 13:21:51Z, first PaperTrade opened 13:21:40Z.
# Cohérence: tous les trades opened_at >= 2026-05-11T13:21:33Z sont dans le
# nouveau régime strict + category_resolver.

# Diagnostic-only period (NOT baseline):
DIAGNOSTIC_PERIOD_START = datetime(2026, 5, 11, 1, 54, 10, tzinfo=UTC)  # strict_cutover_at
DIAGNOSTIC_PERIOD_END = EFFECTIVE_BASELINE_T0


def is_post_effective_baseline(opened_at: datetime | None) -> bool:
    """True if a paper trade opened at this timestamp counts towards baseline."""
    if opened_at is None:
        return False
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=UTC)
    return opened_at >= EFFECTIVE_BASELINE_T0


def baseline_info() -> dict:
    """For /observability/baseline-info endpoint."""
    now = datetime.now(UTC)
    elapsed_h = (now - EFFECTIVE_BASELINE_T0).total_seconds() / 3600
    return {
        "effective_baseline_t0": EFFECTIVE_BASELINE_T0_ISO,
        "elapsed_hours": round(elapsed_h, 2),
        "diagnostic_period": {
            "start": DIAGNOSTIC_PERIOD_START.isoformat(timespec="seconds"),
            "end": DIAGNOSTIC_PERIOD_END.isoformat(timespec="seconds"),
            "duration_hours": (DIAGNOSTIC_PERIOD_END - DIAGNOSTIC_PERIOD_START).total_seconds() / 3600,
            "trades_opened": 0,
            "reason": "UNKNOWN_CATEGORY bug — diagnostic only, NOT baseline",
        },
        "phase_b_critical_milestones": {
            "h+6": (EFFECTIVE_BASELINE_T0.timestamp() + 6 * 3600),
            "h+24": (EFFECTIVE_BASELINE_T0.timestamp() + 24 * 3600),
            "h+48": (EFFECTIVE_BASELINE_T0.timestamp() + 48 * 3600),
        },
    }
