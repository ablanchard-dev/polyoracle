# Phase E — Restart playbook après P0 fixes (2026-05-12)

## État avant restart

- Backend stopped gracefully Phase B (2026-05-12 ~17:42 UTC).
- 8 open positions in DB en attente (state recovery les reprendra).
- DB backup : `data/_backups_pre_p0/polyoracle_pre_p0_20260512T1740Z.db` (1.3 GB).
- Git tag `pre-p0-baseline` (commit `7e84d61`).
- Phase C commit : `ecac6bc` — 8 P0 truth fixes + 59 nouveaux tests.
- Audit baseline : `data/exports/audit_forensic_pre_p0_20260512T1734Z.md`.

## Ordre exécution restart

### 1. Vérifier git propre + sync

```bash
cd /opt/app/polyoracle
git status   # doit montrer 0 modifs uncommitted importantes
git log --oneline -5
```

### 2. Stamp le marker P0_FIX_APPLIED_AT

```bash
cd backend
PYTHONPATH=. .venv/bin/python _set_p0_fix_applied_at.py
```

Confirme dans output :
- `BotState.p0_fix_applied_at = <maintenant>`
- `paper_capital = 318.74€` (préservé, pas reset)

### 3. Backend restart (vrai)

```bash
cd /opt/app/polyoracle/backend
nohup .venv/bin/python dev_server.py > backend.dev.log 2> backend.dev.err.log < /dev/null &
disown
```

### 4. Monitor 5 min initial sanity

```bash
# Wait 30s for boot
sleep 30
# Check backend up
curl -s http://localhost:8000/bot/status | head -20
# Check polling running
curl -s http://localhost:8000/bot/status | jq '.polling_running, .last_polling_at'
# Check open positions (should be 8 recovered)
curl -s http://localhost:8000/paper/positions | jq '. | length'
```

Critères sanity :
- HTTP 200 sur /bot/status
- `polling_running = true`
- `last_polling_at` < 60s old
- 8 open positions recovered

### 5. Monitor 1h post-restart

Critères :
- 0 backend crash
- 30+ new trades opened (à MICRO tier on attend ~50/h cadence)
- New trades ont `EntryPriceAudit` rows (vérifier SQL)
- M1 v5 visibility gate ne refuse pas tous les trades

Quick check SQL :
```sql
SELECT COUNT(*) FROM entrypriceaudit
WHERE created_at >= datetime('now', '-1 hour');

SELECT reason_code, COUNT(*) FROM notradedecision
WHERE created_at >= datetime('now', '-1 hour') AND reason_code = 'VISIBILITY_LEAKAGE_SUSPECT'
GROUP BY reason_code;
```

Si VISIBILITY_LEAKAGE_SUSPECT > 5% des décisions → gate trop strict, faut investiguer.

### 6. Monitor 6h post-restart

Compare avec audit forensic pre-P0 :
- Cadence : ~50 trades/h attendu (similar to pre-P0)
- WR : ≥80% maintained (was 84.57%)
- UNKNOWN_CATEGORY : devrait quasi disparaître (P0.1 fix)
- Distribution `entry_price_source` : majority "GAMMA", minority "WALLET_FROZEN_FALLBACK"

### 7. Cascade revert si régression

**Si crash backend** → backup `data/_backups_pre_p0/polyoracle_pre_p0_20260512T1740Z.db` est le filet :

```bash
# Stop backend
pkill -TERM -f dev_server
sleep 10
# Restore DB
cp data/_backups_pre_p0/polyoracle_pre_p0_20260512T1740Z.db data/polyoracle.db
# Revert P0 commits if needed
git revert ecac6bc  # uniquement si bug code, pas si DB issue
# Restart
cd backend && nohup .venv/bin/python dev_server.py > backend.dev.log 2>&1 &
disown
```

**Si cadence chute >50%** → identifier le P0 fautif :
- Si beaucoup VISIBILITY_LEAKAGE_SUSPECT → P0.3 gate trop strict, ajuster tolerance
- Si beaucoup UNKNOWN_CATEGORY persiste → P0.1 fix incomplet, débugger
- Si peu de trades mais bons résultats → patience, c'est juste les heures creuses

**Si WR drop <80%** → cascade Max Edge (spec.md règle 7) :
1. Mesurer où l'edge fuit
2. Audit wallets actifs
3. Reclass B22 (P1 task)
4. P4-A revalidation (P1 task)
5. Polling 15c/s (post-VPS)

### 8. Phase F démarre J+1 si monitor vert

Une fois 24h post-restart validés (cadence + WR + no crash) :
- B22 incremental update (réutilise trades nouvellement instrumentés)
- P4-A revalidation quasi-ELITE (4-6h sur DB existante)
- Préparer VPS US specs + script déploiement

## Commande complète one-shot

Si tout va bien, on peut lancer tout en une commande :

```bash
cd /opt/app/polyoracle/backend && \
PYTHONPATH=. .venv/bin/python _set_p0_fix_applied_at.py && \
nohup .venv/bin/python dev_server.py > backend.dev.log 2> backend.dev.err.log < /dev/null &
disown
echo "Backend restarted, PID=$(pgrep -f dev_server.py)"
```
