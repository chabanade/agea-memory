#!/bin/bash
# ===========================================
# Backup quotidien Neo4j -> local + S3
# ===========================================
# Cron : 0 4 * * * /opt/agea/scripts/backup-neo4j.sh >> /var/log/agea-backup-neo4j.log 2>&1
#
# Strategie : arret bref + copie du dossier data Neo4j via volume.
# Arret ~30s acceptable a 4h du matin.
# ===========================================

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/opt/agea/backups"
BACKUP_FILE="agea-neo4j-${TIMESTAMP}.tar.gz"
COMPOSE_DIR="/opt/agea/docker"

mkdir -p "$BACKUP_DIR"

echo "[${TIMESTAMP}] Debut backup Neo4j..."

cd "$COMPOSE_DIR"

# 1. Arreter les services dependants
echo "[${TIMESTAMP}] Arret bot + mcp-remote..."
docker compose --env-file ../.env stop bot mcp-remote 2>/dev/null || true

# 2. Arreter Neo4j proprement
echo "[${TIMESTAMP}] Arret Neo4j..."
docker compose --env-file ../.env stop neo4j

# 3. Copier le volume data via un container temporaire alpine
echo "[${TIMESTAMP}] Copie du volume data..."
docker run --rm \
    -v docker_neo4j_data:/data:ro \
    -v "${BACKUP_DIR}:/backup" \
    alpine \
    tar czf "/backup/${BACKUP_FILE}" -C /data .

# 4. Redemarrer tous les services
echo "[${TIMESTAMP}] Redemarrage des services..."
docker compose --env-file ../.env up -d

SIZE=$(du -h "${BACKUP_DIR}/${BACKUP_FILE}" | cut -f1)
echo "[${TIMESTAMP}] Backup cree: ${BACKUP_FILE} (${SIZE})"

# 5. Upload vers S3 Scaleway (si configure)
source /opt/agea/.env 2>/dev/null || true
if command -v aws &> /dev/null && [ "${S3_ACCESS_KEY:-xxx}" != "xxx" ]; then
    AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY}" \
    AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY}" \
    aws s3 cp \
        "${BACKUP_DIR}/${BACKUP_FILE}" \
        "s3://${S3_BUCKET:-agea-backups}/neo4j/${BACKUP_FILE}" \
        --endpoint-url "${S3_ENDPOINT:-https://s3.fr-par.scw.cloud}"
    echo "[${TIMESTAMP}] Upload S3 OK"
else
    echo "[${TIMESTAMP}] Backup local uniquement (S3 non configure)"
fi

# 6. Nettoyage local > 14 jours
find "$BACKUP_DIR" -name "agea-neo4j-*.tar.gz" -mtime +14 -delete

echo "[${TIMESTAMP}] Backup Neo4j termine"
