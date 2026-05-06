#!/usr/bin/env bash
# v0.7.2 P1.2 — split pytest into scoped sub-jobs.
#
# Each sub-job has a hard target runtime budget. The full pytest suite
# was timing out at 240-300s on Windows; running split jobs gives
# faster feedback loops and bounds each tier individually.
#
# Usage:
#   bash scripts/test-all.sh              # run every tier
#   bash scripts/test-all.sh fast         # only the fast tier
#   bash scripts/test-all.sh integration  # one specific tier
#
# Tiers
# -----
# fast        ~ 60s   pure unit tests for the allocator + cluster engine,
#                     scoring helpers, sizing, no DB / no API.
# integration ~ 90s   risk engine + paper trading wiring + signal decision
#                     + composite scoring against an in-memory SQLite.
# discovery   ~120s   market_first / dense_trade_auditor / edge_validation
#                     / lost_gems re-audit. Local fixtures only.
# stability   ~240s   stability_smoke (live API smoke). May skip when
#                     POLYMARKET_OFFLINE=1 is set.
# frontend    ~ 60s   `next build` smoke (skips if no node_modules).
#
# All tiers honour PYTEST_OPTS for ad-hoc pytest flags.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PYTEST_OPTS="${PYTEST_OPTS:--q -x --tb=short}"
PY="${PYTHON:-python}"
TIER="${1:-all}"

run_tier() {
    local label="$1"
    shift
    local target_seconds="$1"
    shift
    echo
    echo "=========================================================="
    echo "  pytest tier: $label  (target ≤ ${target_seconds}s)"
    echo "  $*"
    echo "=========================================================="
    local started
    started=$(date +%s)
    DATABASE_URL= "$PY" -m pytest $PYTEST_OPTS "$@"
    local rc=$?
    local elapsed=$(( $(date +%s) - started ))
    if (( rc == 0 )); then
        echo "  ✓ $label OK in ${elapsed}s"
    else
        echo "  ✗ $label FAILED (rc=$rc) after ${elapsed}s"
    fi
    return $rc
}

run_fast() {
    run_tier "fast" 60 \
        backend/app/tests/test_capital_allocator.py \
        backend/app/tests/test_signal_cluster_engine.py \
        backend/app/tests/test_no_mode_names_in_codebase.py \
        backend/app/tests/test_signal_decision.py \
        backend/app/tests/test_composite_wallet_score.py \
        backend/app/tests/test_edge_quality_engine.py \
        backend/app/tests/test_copyable_edge_engine.py \
        backend/app/tests/test_orderbook_analyzer.py
}

run_integration() {
    run_tier "integration" 90 \
        backend/app/tests/test_risk_engine.py \
        backend/app/tests/test_risk_engine_no_trade_log.py \
        backend/app/tests/test_risk_engine_integration_v0_5_8_1.py \
        backend/app/tests/test_risk_modes.py \
        backend/app/tests/test_paper_trading_engine.py \
        backend/app/tests/test_paper_auto_trading.py \
        backend/app/tests/test_paper_close_paths.py \
        backend/app/tests/test_bot_modes.py \
        backend/app/tests/test_bot_loop.py
}

run_discovery() {
    run_tier "discovery" 120 \
        backend/app/tests/test_market_first_discovery.py \
        backend/app/tests/test_dense_trade_auditor.py \
        backend/app/tests/test_discovery_audit.py \
        backend/app/tests/test_edge_validation_engine.py \
        backend/app/tests/test_candidate_validation.py \
        backend/app/tests/test_lost_gems_re_audit.py \
        backend/app/tests/test_validated_paper_universe.py \
        backend/app/tests/test_validated_paper_universe_v0_5_6.py \
        backend/app/tests/test_phase_b_modules.py \
        backend/app/tests/test_polymarket_normalizers.py \
        backend/app/tests/test_compliance_config.py
}

run_stability() {
    run_tier "stability" 240 \
        backend/app/tests/test_stability_smoke.py
}

run_frontend() {
    if [[ ! -d frontend/node_modules ]]; then
        echo "  ⚠ frontend tier skipped (frontend/node_modules missing — run 'npm install' first)"
        return 0
    fi
    echo
    echo "=========================================================="
    echo "  frontend tier: next build  (target ≤ 60s)"
    echo "=========================================================="
    local started
    started=$(date +%s)
    (cd frontend && npm run build > /tmp/_v0_7_2_frontend_build.log 2>&1)
    local rc=$?
    local elapsed=$(( $(date +%s) - started ))
    if (( rc == 0 )); then
        echo "  ✓ frontend OK in ${elapsed}s"
    else
        echo "  ✗ frontend FAILED (rc=$rc) after ${elapsed}s"
        tail -25 /tmp/_v0_7_2_frontend_build.log || true
    fi
    return $rc
}

case "$TIER" in
    fast)        run_fast ;;
    integration) run_integration ;;
    discovery)   run_discovery ;;
    stability)   run_stability ;;
    frontend)    run_frontend ;;
    all|"")
        rc_total=0
        run_fast        || rc_total=$?
        run_integration || rc_total=$?
        run_discovery   || rc_total=$?
        run_stability   || rc_total=$?
        run_frontend    || rc_total=$?
        if (( rc_total == 0 )); then
            echo
            echo "  ALL TIERS OK"
        else
            echo
            echo "  ONE OR MORE TIERS FAILED (rc=$rc_total)"
        fi
        exit "$rc_total"
        ;;
    *)
        echo "Unknown tier: $TIER"
        echo "Valid: fast | integration | discovery | stability | frontend | all"
        exit 2
        ;;
esac
