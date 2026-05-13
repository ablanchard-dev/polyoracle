#!/bin/bash
# POLYORACLE — déploiement VPS Hostinger Boston (ou compatible Ubuntu 24.04)
# Usage: à exécuter SUR le VPS après SSH-ing in en tant que root ou sudo user
#
# Pré-requis local (sur ton PC Lyon) avant ce script:
#   1. ssh-copy-id alex@<IP_VPS> (= ta clé SSH publique installée sur le VPS)
#   2. Stop bot local: kill -TERM $(pgrep -f dev_server.py)
#   3. Snapshot local DB: cp data/polyoracle.db data/_backup_pre_migration_$(date +%s).db
#   4. Transfer DB vers VPS: scp data/polyoracle.db alex@<IP_VPS>:~/polyoracle/data/polyoracle.db
#
# Ce script ne lance PAS le bot — il prépare l'environnement.
# Le start du bot est manuel après vérification.

set -euo pipefail

POLYORACLE_USER="${POLYORACLE_USER:-dexter}"
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
sudo -u "$POLYORACLE_USER" bash << EOF
set -e
cd /home/${POLYORACLE_USER}
if [ ! -d polyoracle ]; then
    git clone https://github.com/ablanchard-dev/polyoracle.git
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
MemoryMax=2G
MemoryHigh=1500M

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
# Hourly SQLite snapshot with 24-snapshot retention
BACKUP_DIR="${POLYORACLE_HOME}/data/_hourly_backups"
DB="${POLYORACLE_HOME}/data/polyoracle.db"
sudo -u "${POLYORACLE_USER}" sqlite3 "\$DB" ".backup '\$BACKUP_DIR/polyoracle_\$(date +%Y%m%dT%H00Z).db'"
# Retention: keep last 24 hourly + 7 daily
sudo -u "${POLYORACLE_USER}" find "\$BACKUP_DIR" -name 'polyoracle_*.db' -mtime +7 -delete
CRON
chmod +x /etc/cron.hourly/polyoracle-backup

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
