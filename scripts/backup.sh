#!/bin/bash
# ===========================================
# Backup quotidien PostgreSQL -> local + S3
# ===========================================
# Cron : 0 2 * * * /opt/agea/scripts/backup.sh >> /var/log/agea-backup.log 2>&1
# ===========================================

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/opt/agea/backups"
BACKUP_FILE="agea-pg-${TIMESTAMP}.sql.gz"

# Creer le dossier
mkdir -p "$BACKUP_DIR"

echo "[${TIMESTAMP}] Debut backup PostgreSQL..."

# Dump PostgreSQL via docker compose (nom du service, pas du container)
cd /opt/agea/docker
docker compose --env-file ../.env exec -T postgres pg_dump \
    -U "${POSTGRES_USER:-agea}" \
    -d "${POSTGRES_DB:-agea_memory}" \
    --format=plain \
    | gzip > "${BACKUP_DIR}/${BACKUP_FILE}"

SIZE=$(du -h "${BACKUP_DIR}/${BACKUP_FILE}" | cut -f1)
echo "[${TIMESTAMP}] Dump cree: ${BACKUP_FILE} (${SIZE})"

# Upload vers S3 Scaleway (optionnel, si aws CLI installe)
if command -v aws &> /dev/null && [ "${S3_ACCESS_KEY:-xxx}" != "xxx" ]; then
    AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY}" \
    AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY}" \
    aws s3 cp \
        "${BACKUP_DIR}/${BACKUP_FILE}" \
        "s3://${S3_BUCKET:-agea-backups}/${BACKUP_FILE}" \
        --endpoint-url "${S3_ENDPOINT:-https://s3.fr-par.scw.cloud}"
    echo "[${TIMESTAMP}] Upload S3 OK"
else
    echo "[${TIMESTAMP}] Backup local uniquement (S3 non configure)"
fi

# Nettoyage backups locaux > 7 jours
find "$BACKUP_DIR" -name "agea-pg-*.sql.gz" -mtime +7 -delete

echo "[${TIMESTAMP}] Backup termine"
