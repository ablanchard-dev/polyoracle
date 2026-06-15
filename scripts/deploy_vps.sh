#!/bin/bash
# POLYORACLE — déploiement VPS Hostinger Boston (ou compatible Ubuntu 24.04)
# Usage: à exécuter SUR le VPS après SSH-ing in en tant que root ou sudo user
#
# Pré-requis local (sur ton PC Lyon) avant ce script:
#   1. ssh-copy-id root@<IP_VPS> (= ta clé SSH publique installée sur le VPS)
#   2. Sur le VPS : générer une SSH key ed25519 et l'ajouter comme deploy key
#      (read+write) sur le repo GitHub privé ablanchard-dev/polyoracle.
#      Repo est PRIVÉ donc HTTPS clone échoue — SSH obligatoire.
#   3. Stop bot local: kill -TERM $(pgrep -f dev_server.py)
#   4. Snapshot local DB: cp data/polyoracle.db data/_backup_pre_migration_$(date +%s).db
#   5. Transfer DB vers VPS: scp data/polyoracle.db root@<IP_VPS>:/home/polyoracle/polyoracle/data/polyoracle.db
#
# Ce script ne lance PAS le bot — il prépare l'environnement.
# Le start du bot est manuel après vérification.

set -euo pipefail

POLYORACLE_USER="${POLYORACLE_USER:-polyoracle}"
POLYORACLE_HOME="/home/${POLYORACLE_USER}/polyoracle"

echo "=== POLYORACLE VPS deployment ==="
echo "User: $POLYORACLE_USER  Home: $POLYORACLE_HOME"

# 1. System packages
echo ""
echo ">>> [1/8] System packages"
apt-get update
apt-get install -y \
    python3.12 python3.12-venv python3.12-dev \
    git curl wget htop tmux \
    sqlite3 \
    build-essential \
    ufw fail2ban

# 2. Firewall — only SSH + backend
echo ""
echo ">>> [2/8] UFW firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 8000/tcp comment 'polyoracle-backend'
ufw --force enable
ufw status verbose

# 3. fail2ban (anti brute-force SSH)
echo ""
echo ">>> [3/8] fail2ban"
systemctl enable --now fail2ban

# 4. Create polyoracle user if not exists
echo ""
echo ">>> [4/8] User polyoracle"
if ! id -u "$POLYORACLE_USER" >/dev/null 2>&1; then
    adduser --gecos "" --disabled-password "$POLYORACLE_USER"
    usermod -aG sudo "$POLYORACLE_USER"
fi

# 5. Repo + venv
echo ""
echo ">>> [5/8] Clone repo + venv"
# Repo polyoracle est PRIVÉ. HTTPS = échec. SSH obligatoire.
# Pré-requis : deploy key (ed25519, read+write) générée sur le VPS et installée
# dans GitHub > Settings > Deploy keys du repo.
#
# Pour générer la clé sur le VPS (si pas déjà fait, root) :
#   sudo -u ${POLYORACLE_USER} ssh-keygen -t ed25519 -N '' -f /home/${POLYORACLE_USER}/.ssh/id_ed25519
#   cat /home/${POLYORACLE_USER}/.ssh/id_ed25519.pub
#   # → copier la clé publique dans GitHub > repo > Settings > Deploy keys (write enabled)
#
# Puis tester :
#   sudo -u ${POLYORACLE_USER} ssh -T git@github.com
sudo -u "$POLYORACLE_USER" bash << EOF
set -e
cd /home/${POLYORACLE_USER}
if [ ! -d polyoracle ]; then
    # SSH clone obligatoire (repo privé). Si la deploy key n'est pas installée,
    # ce step échoue avec "Permission denied (publickey)" — installer la deploy
    # key avant de re-run.
    git clone git@github.com:ablanchard-dev/polyoracle.git
fi
cd polyoracle/backend
if [ ! -d .venv ]; then
    python3.12 -m venv .venv
fi
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt 2>/dev/null || .venv/bin/pip install fastapi uvicorn sqlmodel sqlalchemy pydantic pydantic-settings httpx aiohttp pyyaml
EOF

# 6. systemd unit
echo ""
echo ">>> [6/8] systemd polyoracle-backend.service"
cat > /etc/systemd/system/polyoracle-backend.service << UNIT
[Unit]
Description=POLYORACLE backend (FastAPI + polling)
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=${POLYORACLE_USER}
WorkingDirectory=${POLYORACLE_HOME}/backend
Environment=PYTHONPATH=${POLYORACLE_HOME}/backend
EnvironmentFile=${POLYORACLE_HOME}/backend/.env
ExecStart=${POLYORACLE_HOME}/backend/.venv/bin/python dev_server.py
Restart=always
RestartSec=10
StandardOutput=append:${POLYORACLE_HOME}/backend/backend.dev.log
StandardError=append:${POLYORACLE_HOME}/backend/backend.dev.err.log

# RSS guard (D5)
MemoryMax=3G
MemoryHigh=2G

# WatchdogSec DISABLED — le bot n'implémente pas sd_notify(WATCHDOG=1).
# Activer Watchdog sans sd_notify = systemd kill périodique malgré bot sain.
# Restart=always suffit (déclenche sur vrai crash / exit non-zero).
# WatchdogSec=600s
TimeoutStartSec=600s
TimeoutStopSec=30s

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable polyoracle-backend.service

# 7. Backup cron (hourly DB snapshot)
echo ""
echo ">>> [7/8] DB backup cron"
mkdir -p "${POLYORACLE_HOME}/data/_hourly_backups"
chown "$POLYORACLE_USER:$POLYORACLE_USER" "${POLYORACLE_HOME}/data/_hourly_backups"

cat > /etc/cron.hourly/polyoracle-backup << CRON
#!/bin/bash
# Hourly SQLite snapshot avec disk pressure guard + retention 24 max.
# Postmortem 2026-05-13 : sans disk guard + retention agressive, un restart
# loop systemd + auto-reclass loop ont produit 132×1.4G = 180 GB et crashé
# le VPS. Garde-fous obligatoires.
BACKUP_DIR="${POLYORACLE_HOME}/data/_hourly_backups"
DB="${POLYORACLE_HOME}/data/polyoracle.db"

# Disk pressure guard : skip si <5G free sur le mount du backup dir
FREE_G=\$(df -BG "\$BACKUP_DIR" | awk 'NR==2 {gsub("G","",\$4); print \$4}')
if [ "\$FREE_G" -lt 5 ]; then
    logger -t polyoracle-backup "skip: <5G free (\$FREE_G G)"
    exit 0
fi

# Snapshot
sudo -u "${POLYORACLE_USER}" sqlite3 "\$DB" ".backup '\$BACKUP_DIR/polyoracle_\$(date +%Y%m%dT%H00Z).db'"

# Retention 24 max : keep newest 24 fichiers, delete le reste
sudo -u "${POLYORACLE_USER}" bash -c "ls -1t '\$BACKUP_DIR'/polyoracle_*.db 2>/dev/null | tail -n +25 | xargs -r rm -f"
CRON
chmod +x /etc/cron.hourly/polyoracle-backup

# 7b. Reclass backup retention (daily cleanup, 7 keep max)
echo ""
echo ">>> [7b/8] Reclass backup retention"
mkdir -p "${POLYORACLE_HOME}/data/_reclass_backups"
chown "$POLYORACLE_USER:$POLYORACLE_USER" "${POLYORACLE_HOME}/data/_reclass_backups"

cat > /etc/cron.daily/polyoracle-reclass-cleanup << CRON
#!/bin/bash
# Reclass backups : keep 7 newest only (postmortem 2026-05-13).
BACKUP_DIR="${POLYORACLE_HOME}/data/_reclass_backups"
sudo -u "${POLYORACLE_USER}" bash -c "ls -1t '\$BACKUP_DIR'/polyoracle_*.db 2>/dev/null | tail -n +8 | xargs -r rm -f"
CRON
chmod +x /etc/cron.daily/polyoracle-reclass-cleanup

# 8. Final checks
echo ""
echo ">>> [8/8] Final checks"
echo "  - Python: \$(python3.12 --version 2>&1)"
echo "  - Polyoracle dir: \$(ls -la ${POLYORACLE_HOME}/backend/dev_server.py 2>&1 | head -1)"
echo "  - DB present: \$(ls -lh ${POLYORACLE_HOME}/data/polyoracle.db 2>&1 | head -1)"
echo "  - systemd unit: \$(systemctl status polyoracle-backend.service --no-pager 2>&1 | head -3)"
echo "  - UFW: \$(ufw status 2>&1 | head -4)"

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Next manual steps:"
echo "  1. Transfer DB from local PC:"
echo "     scp data/polyoracle.db ${POLYORACLE_USER}@<IP_VPS>:${POLYORACLE_HOME}/data/polyoracle.db"
echo "  2. Transfer .env (with secrets):"
echo "     scp backend/.env ${POLYORACLE_USER}@<IP_VPS>:${POLYORACLE_HOME}/backend/.env"
echo "  3. Start backend:"
echo "     ssh ${POLYORACLE_USER}@<IP_VPS> 'sudo systemctl start polyoracle-backend'"
echo "  4. Verify:"
echo "     curl http://<IP_VPS>:8000/bot/status"
echo "  5. Start polling:"
echo "     curl -X POST http://<IP_VPS>:8000/bot/polling/start"
