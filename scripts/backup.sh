#!/bin/bash
# ===========================================
# Backup quotidien PostgreSQL -> S3 Scaleway
# ===========================================
# Cron : 0 2 * * * /opt/agea/scripts/backup.sh
# ===========================================

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/tmp/agea-backups"
BACKUP_FILE="agea-pg-${TIMESTAMP}.sql.gz"

# Creer le dossier temporaire
mkdir -p "$BACKUP_DIR"

echo "[${TIMESTAMP}] Debut backup PostgreSQL..."

# Dump PostgreSQL depuis le container Docker
docker exec agea-postgres pg_dump \
    -U "${POSTGRES_USER:-agea}" \
    -d "${POSTGRES_DB:-agea_memory}" \
    --format=plain \
    | gzip > "${BACKUP_DIR}/${BACKUP_FILE}"

echo "[${TIMESTAMP}] Dump cree: ${BACKUP_FILE}"

# Upload vers S3 Scaleway
if command -v aws &> /dev/null; then
    aws s3 cp \
        "${BACKUP_DIR}/${BACKUP_FILE}" \
        "s3://${S3_BUCKET:-agea-backups}/${BACKUP_FILE}" \
        --endpoint-url "${S3_ENDPOINT:-https://s3.fr-par.scw.cloud}"
    echo "[${TIMESTAMP}] Upload S3 OK"
else
    echo "[${TIMESTAMP}] WARN: aws CLI absent, backup local uniquement"
fi

# Nettoyage backups locaux > 7 jours
find "$BACKUP_DIR" -name "agea-pg-*.sql.gz" -mtime +7 -delete

echo "[${TIMESTAMP}] Backup termine"
