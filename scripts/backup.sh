#!/bin/bash
# ===========================================
# Backup quotidien PostgreSQL -> local (+ S3 si configure)
# ===========================================
# Cron : 0 2 * * * /opt/agea/scripts/backup.sh >> /var/log/agea-backup.log 2>&1
#
# Strategie : pg_dump A CHAUD (postgres reste UP, pas d'arret requis).
# Seul service touche = lecture de la DB. Zero impact sur bot/mcp-remote.
#
# Garde-fous :
#   - trap ERR qui alerte Telegram si le dump foire
#   - sanity check taille > 10 Ko (un dump vide fait quelques octets)
#   - upload S3 non-bloquant (echec S3 != echec global)
# ===========================================

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/opt/agea/backups"
BACKUP_FILE="agea-pg-${TIMESTAMP}.sql.gz"
COMPOSE_DIR="/opt/agea/docker"
ENV_FILE="/opt/agea/.env"
LOG_PREFIX="[backup-pg ${TIMESTAMP}]"

mkdir -p "$BACKUP_DIR"

log() { echo "${LOG_PREFIX} $*"; }

set -a
# shellcheck disable=SC1090
source "$ENV_FILE" 2>/dev/null || true
set +a

telegram_alert() {
    local msg=$1
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${MEHDI_CHAT_ID:-}" ]; then
        log "SKIP Telegram (credentials absents)"
        return
    fi
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${MEHDI_CHAT_ID}" \
        --data-urlencode "text=${msg}" \
        --data-urlencode "parse_mode=HTML" > /dev/null || true
}

on_failure() {
    local exit_code=$?
    log "ERROR exit=$exit_code"
    telegram_alert "🚨 <b>AGEA backup PostgreSQL echoue</b>
${TIMESTAMP}
exit_code=${exit_code}
Postgres n'a pas ete stoppe (pg_dump a chaud) donc aucune action de remise en etat.
Dernier backup sain : $(ls -t /opt/agea/backups/agea-pg-*.sql.gz 2>/dev/null | head -1 | xargs -I{} basename {} 2>/dev/null || echo 'AUCUN').
Intervention manuelle requise."
    exit "$exit_code"
}
trap on_failure ERR

log "Debut backup PostgreSQL (pg_dump a chaud)"
cd "$COMPOSE_DIR"

# pg_dump via docker compose exec. -T = pas de TTY (cron-safe).
docker compose --env-file "$ENV_FILE" exec -T postgres pg_dump \
    -U "${POSTGRES_USER:-agea}" \
    -d "${POSTGRES_DB:-agea_memory}" \
    --format=plain \
    | gzip > "${BACKUP_DIR}/${BACKUP_FILE}"

SIZE=$(du -h "${BACKUP_DIR}/${BACKUP_FILE}" | cut -f1)
SIZE_BYTES=$(stat -c %s "${BACKUP_DIR}/${BACKUP_FILE}")
log "Dump local cree: ${BACKUP_FILE} (${SIZE}, ${SIZE_BYTES} bytes)"

# Sanity check : dump < 10 Ko => schema+data probablement absents
if [ "$SIZE_BYTES" -lt 10240 ]; then
    log "ERROR dump trop petit (< 10 Ko) - pg_dump a probablement echoue"
    rm -f "${BACKUP_DIR}/${BACKUP_FILE}"
    false  # declenche trap
fi

# Upload S3 non-bloquant
if command -v aws &> /dev/null && [ "${S3_ACCESS_KEY:-xxx}" != "xxx" ]; then
    log "Upload S3..."
    if AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY}" \
       AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY}" \
       aws s3 cp \
           "${BACKUP_DIR}/${BACKUP_FILE}" \
           "s3://${S3_BUCKET:-agea-backups}/postgres/${BACKUP_FILE}" \
           --endpoint-url "${S3_ENDPOINT:-https://s3.fr-par.scw.cloud}" >> /dev/null 2>&1; then
        log "Upload S3 OK"
    else
        log "WARN upload S3 echoue (backup local preserve)"
        telegram_alert "⚠️ <b>AGEA backup Postgres : S3 upload echoue</b>
${TIMESTAMP}
Backup local OK (${SIZE}) mais copie off-site echouee."
    fi
else
    log "S3 non configure. Backup LOCAL UNIQUEMENT."
fi

# Rotation locale > 7 jours
find "$BACKUP_DIR" -name "agea-pg-*.sql.gz" -mtime +7 -delete 2>/dev/null || true

log "Backup PostgreSQL termine OK"
