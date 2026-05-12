#!/bin/bash
# D8 chaos test (Phase D 2026-05-11).
# Kill backend mid-smoke + verify state recovery.
#
# Critère go: kill -9 backend pendant N positions ouvertes →
#   reprise automatique (D2 systemd) → 0 position perdue, 0 duplicate.
#
# Usage: bash scripts/chaos/D8_chaos_kill_backend.sh
#
# WARNING: only run AFTER smoke baseline + on a separate test DB. Never
# run during a production-grade smoke (this is a destructive chaos drill).

set -u
ROOT="/opt/app/polyoracle"
LOG="$ROOT/backend/_smoke_strict_logs/chaos_test_$(date -u +%Y%m%dT%H%MZ).log"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

log "=== D8 chaos: kill backend test starting ==="

# 1. Snapshot pre-chaos state
PRE_OPEN=$(curl -s http://localhost:8000/paper/positions 2>&1 | grep -oE '"id":"[^"]+"' | wc -l || echo "?")
PRE_CUTOVER=$(curl -s http://localhost:8000/bot/strict-cutover 2>&1 | grep -oE '"trades_after_cutover":[0-9]+' | head -1 | cut -d: -f2)
PRE_PID=$(pgrep -f "dev_server.py" | head -1)
log "PRE: open_positions=$PRE_OPEN trades_after_cutover=$PRE_CUTOVER backend_pid=$PRE_PID"

if [ -z "$PRE_PID" ]; then
    log "FAIL: backend not running, abort chaos test"
    exit 1
fi

# 2. SIGKILL backend
log "Sending SIGKILL to PID $PRE_PID"
kill -9 "$PRE_PID"
sleep 2

# 3. Wait for systemd auto-restart (D1 polyoracle-backend.service)
log "Waiting for systemd auto-restart (Restart=always RestartSec=10)..."
RECOVERY_TIMEOUT=60
START_TS=$(date +%s)
while true; do
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/bot/status --max-time 3 | grep -q "^200$"; then
        ELAPSED=$(( $(date +%s) - START_TS ))
        log "OK: backend back up after ${ELAPSED}s"
        break
    fi
    if [ $(( $(date +%s) - START_TS )) -gt $RECOVERY_TIMEOUT ]; then
        log "FAIL: backend not back after ${RECOVERY_TIMEOUT}s — D2 systemd watchdog broken or not installed"
        exit 1
    fi
    sleep 2
done

# 4. Verify state recovery
NEW_PID=$(pgrep -f "dev_server.py" | head -1)
log "NEW backend PID: $NEW_PID (was $PRE_PID)"

# 5. Check positions count preserved
POST_OPEN=$(curl -s http://localhost:8000/paper/positions 2>&1 | grep -oE '"id":"[^"]+"' | wc -l || echo "?")
POST_CUTOVER=$(curl -s http://localhost:8000/bot/strict-cutover 2>&1 | grep -oE '"trades_after_cutover":[0-9]+' | head -1 | cut -d: -f2)
log "POST: open_positions=$POST_OPEN trades_after_cutover=$POST_CUTOVER"

# 6. Verdict
if [ "$PRE_OPEN" = "$POST_OPEN" ] && [ "$PRE_CUTOVER" = "$POST_CUTOVER" ]; then
    log "PASS: state preserved across kill -9 + restart"
    exit 0
else
    log "FAIL: state divergence (open: $PRE_OPEN→$POST_OPEN, cutover_trades: $PRE_CUTOVER→$POST_CUTOVER)"
    exit 1
fi
