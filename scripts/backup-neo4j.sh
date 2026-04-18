#!/bin/bash
# ===========================================
# Backup quotidien Neo4j -> local (+ S3 si configure)
# ===========================================
# Cron : 0 4 * * * /opt/agea/scripts/backup-neo4j.sh >> /var/log/agea-backup-neo4j.log 2>&1
#
# Strategie Option A (Neo4j Community 5.26 - dump a chaud impossible) :
#   1. Stop de neo4j UNIQUEMENT (bot et mcp-remote restent UP => pas de coupure MCP clients)
#   2. tar.gz du volume docker_neo4j_data en mode read-only
#   3. Restart neo4j via `up -d --no-deps neo4j` (jamais d'up-d global)
#   4. Trap ERR : si une etape foire, tente de remonter neo4j et alerte Telegram
#
# Arret neo4j attendu : ~20-30s (backup + restart healthy). Bot continue a repondre
# /health ; /api/facts echoue temporairement (transactions Graphiti en attente).
# ===========================================

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/opt/agea/backups"
BACKUP_FILE="agea-neo4j-${TIMESTAMP}.tar.gz"
COMPOSE_DIR="/opt/agea/docker"
ENV_FILE="/opt/agea/.env"
LOG_PREFIX="[backup-neo4j ${TIMESTAMP}]"

mkdir -p "$BACKUP_DIR"

log() { echo "${LOG_PREFIX} $*"; }

# Charge .env pour TELEGRAM_BOT_TOKEN, MEHDI_CHAT_ID, S3_*
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

# Trap : neo4j down + echec => on remonte neo4j et on crie.
on_failure() {
    local exit_code=$?
    log "ERROR exit=$exit_code, tentative de remise en etat..."
    cd "$COMPOSE_DIR" 2>/dev/null || true
    # up -d --no-deps neo4j = on NE recree PAS bot/mcp-remote/caddy qui n'ont pas bouge
    docker compose --env-file "$ENV_FILE" up -d --no-deps neo4j >> /dev/null 2>&1 || true
    telegram_alert "🚨 <b>AGEA backup Neo4j echoue</b>
${TIMESTAMP}
exit_code=${exit_code}
neo4j remonte en urgence via up -d --no-deps.
Dernier backup sain : /opt/agea/backups/$(ls -t /opt/agea/backups/agea-neo4j-*.tar.gz 2>/dev/null | head -1 | xargs -I{} basename {}).
Intervention manuelle requise."
    exit "$exit_code"
}
trap on_failure ERR

log "Debut backup Neo4j (Option A --no-deps)"
cd "$COMPOSE_DIR"

# 1. Stop neo4j UNIQUEMENT — pas de stop bot / mcp-remote / caddy
log "Stop neo4j (seul)"
docker compose --env-file "$ENV_FILE" stop neo4j

# 2. Copie du volume data via alpine read-only
log "tar.gz du volume docker_neo4j_data"
docker run --rm \
    -v docker_neo4j_data:/data:ro \
    -v "${BACKUP_DIR}:/backup" \
    alpine \
    tar czf "/backup/${BACKUP_FILE}" -C /data .

# 3. Redemarre neo4j SANS --deps (--no-deps) et SANS toucher aux autres services
log "Restart neo4j (--no-deps)"
docker compose --env-file "$ENV_FILE" up -d --no-deps neo4j

# 4. Attendre healthy (timeout 90s)
log "Attente neo4j healthy..."
for i in $(seq 1 18); do
    sleep 5
    status=$(docker inspect docker-neo4j-1 --format '{{.State.Health.Status}}' 2>/dev/null || echo "starting")
    if [ "$status" = "healthy" ]; then
        log "neo4j healthy apres ${i}x5s"
        break
    fi
    if [ "$i" = "18" ]; then
        log "ERROR : neo4j PAS healthy apres 90s"
        false  # declenche le trap
    fi
done

SIZE=$(du -h "${BACKUP_DIR}/${BACKUP_FILE}" | cut -f1)
SIZE_BYTES=$(stat -c %s "${BACKUP_DIR}/${BACKUP_FILE}")
log "Backup local cree: ${BACKUP_FILE} (${SIZE})"

# 5. Sanity check : taille > 1 Mo sinon backup suspect
if [ "$SIZE_BYTES" -lt 1048576 ]; then
    log "ERROR : backup < 1 Mo (${SIZE_BYTES} bytes) - corruption probable"
    false  # declenche le trap
fi

# 6. Upload S3 Scaleway (si configure). Pas un echec bloquant si S3 tombe.
if command -v aws &> /dev/null && [ "${S3_ACCESS_KEY:-xxx}" != "xxx" ]; then
    log "Upload S3..."
    if AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY}" \
       AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY}" \
       aws s3 cp \
           "${BACKUP_DIR}/${BACKUP_FILE}" \
           "s3://${S3_BUCKET:-agea-backups}/neo4j/${BACKUP_FILE}" \
           --endpoint-url "${S3_ENDPOINT:-https://s3.fr-par.scw.cloud}" >> /dev/null 2>&1; then
        log "Upload S3 OK"
    else
        log "WARN : upload S3 echoue (backup local preserve)"
        telegram_alert "⚠️ <b>AGEA backup Neo4j : S3 upload echoue</b>
${TIMESTAMP}
Backup local OK (${SIZE}) mais copie off-site echouee.
Risque : panne VPS = perte des donnees depuis dernier upload S3 sain."
    fi
else
    log "S3 non configure (cles a 'xxx' ou aws cli absent). Backup LOCAL UNIQUEMENT."
fi

# 7. Rotation locale > 14 jours
find "$BACKUP_DIR" -name "agea-neo4j-*.tar.gz" -mtime +14 -delete 2>/dev/null || true

log "Backup Neo4j termine OK"
