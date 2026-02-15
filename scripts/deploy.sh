#!/bin/bash
# ===========================================
# Deploiement AGEA sur VPS Hostinger
# ===========================================
# Usage : ./scripts/deploy.sh
# ===========================================

set -euo pipefail

VPS_IP="${VPS_IP:-148.230.112.42}"
VPS_USER="${VPS_USER:-root}"
REMOTE_DIR="/opt/agea"

echo "=== Deploiement AGEA sur ${VPS_IP} ==="

# 1. Synchroniser les fichiers
echo "[1/4] Synchronisation des fichiers..."
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='.env' \
    ./docker/ ./bot/ ./scripts/ \
    "${VPS_USER}@${VPS_IP}:${REMOTE_DIR}/"

# 2. Copier le .env si absent sur le VPS
echo "[2/4] Verification du .env..."
ssh "${VPS_USER}@${VPS_IP}" "test -f ${REMOTE_DIR}/.env || echo 'ATTENTION: .env absent sur le VPS !'"

# 3. Build et redemarrage
echo "[3/4] Build et redemarrage Docker..."
ssh "${VPS_USER}@${VPS_IP}" "cd ${REMOTE_DIR} && docker compose build --no-cache bot && docker compose up -d"

# 4. Verification
echo "[4/4] Verification sante..."
sleep 5
ssh "${VPS_USER}@${VPS_IP}" "docker compose -f ${REMOTE_DIR}/docker-compose.yml ps"

echo "=== Deploiement termine ==="
echo "Bot: https://${BOT_DOMAIN:-agea.hexagon-enr.fr}/status"
echo "Zep: https://${DOMAIN:-memory.hexagon-enr.fr}/healthz"
