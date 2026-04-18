#!/bin/bash
# Self-heal AGEA stack - HEXAGONE ENERGIE
# Tourne toutes les 30 min via cron. Detecte + repare automatiquement les services en panne.
# N'alerte sur Telegram QU'EN CAS d'echec de reparation automatique.
# Anti-spam: 1 alerte max toutes les 6h par probleme identique.

set -uo pipefail

# === CONFIG ===
AGEA_DIR="/opt/agea"
N8N_DIR="/opt/n8n"
INVOICENINJA_DIR="/opt/invoiceninja"
STATE_DIR="/var/lib/agea-self-heal"
LOG_FILE="/var/log/agea-self-heal.log"
STATE_FILE="$STATE_DIR/state.json"
ALERT_COOLDOWN=21600  # 6h
HEALTH_URL_EXTERNAL="https://srv987452.hstgr.cloud"
LOG_PREFIX="[AGEA self-heal]"

# === INIT ===
mkdir -p "$STATE_DIR"
[ -f "$STATE_FILE" ] || echo '{"alerts":{}}' > "$STATE_FILE"

set -a
# shellcheck disable=SC1091
source "$AGEA_DIR/.env" 2>/dev/null || true
set +a

ALERTS_PENDING=()
RECOVERED=()
TEST_MODE=false
for arg in "$@"; do
    [ "$arg" = "--test" ] && TEST_MODE=true
done

log() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') $LOG_PREFIX $*" >> "$LOG_FILE"
    [ -t 1 ] && echo "$LOG_PREFIX $*"
}

telegram_send() {
    local msg=$1
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${MEHDI_CHAT_ID:-}" ]; then
        log "SKIP Telegram (token ou chat_id manquant)"
        return
    fi
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${MEHDI_CHAT_ID}" \
        --data-urlencode "text=${msg}" \
        --data-urlencode "parse_mode=HTML" > /dev/null
}

should_alert() {
    local key=$1
    local now
    now=$(date +%s)
    local last
    last=$(jq -r --arg k "$key" '.alerts[$k] // 0' "$STATE_FILE" 2>/dev/null || echo 0)
    [ $((now - last)) -gt "$ALERT_COOLDOWN" ]
}

mark_alerted() {
    local key=$1
    local now
    now=$(date +%s)
    local tmp
    tmp=$(mktemp)
    jq --arg k "$key" --argjson t "$now" '.alerts[$k] = $t' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
}

clear_alert() {
    local key=$1
    local was_alerting
    was_alerting=$(jq -r --arg k "$key" '.alerts[$k] // empty' "$STATE_FILE" 2>/dev/null)
    if [ -n "$was_alerting" ]; then
        RECOVERED+=("$key")
        local tmp
        tmp=$(mktemp)
        jq --arg k "$key" 'del(.alerts[$k])' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
    fi
}

container_exists() {
    docker ps -a --format '{{.Names}}' | grep -qx "$1"
}

container_running() {
    docker ps --format '{{.Names}}' | grep -qx "$1"
}

# === CHECKS ===

check_postgres() {
    if docker exec docker-postgres-1 pg_isready -q 2>/dev/null; then
        clear_alert "postgres"
        return 0
    fi
    log "Postgres not ready, restart..."
    (cd "$AGEA_DIR/docker" && docker compose restart postgres >> "$LOG_FILE" 2>&1)
    sleep 20
    if docker exec docker-postgres-1 pg_isready -q 2>/dev/null; then
        log "Postgres RECOVERED"
        clear_alert "postgres"
        return 0
    fi
    log "Postgres STILL DOWN"
    ALERTS_PENDING+=("postgres::Postgres non-répondant après restart")
    return 1
}

check_neo4j() {
    local pwd_neo4j="${NEO4J_PASSWORD:-}"
    if [ -z "$pwd_neo4j" ]; then
        log "NEO4J_PASSWORD vide, skip neo4j check"
        return 0
    fi
    if docker exec docker-neo4j-1 cypher-shell -u neo4j -p "$pwd_neo4j" "RETURN 1" > /dev/null 2>&1; then
        clear_alert "neo4j"
        return 0
    fi
    log "Neo4j not responding, restart..."
    (cd "$AGEA_DIR/docker" && docker compose restart neo4j >> "$LOG_FILE" 2>&1)
    sleep 35
    if docker exec docker-neo4j-1 cypher-shell -u neo4j -p "$pwd_neo4j" "RETURN 1" > /dev/null 2>&1; then
        log "Neo4j RECOVERED"
        clear_alert "neo4j"
        return 0
    fi
    log "Neo4j STILL DOWN"
    ALERTS_PENDING+=("neo4j::Neo4j non-répondant après restart")
    return 1
}

lexia_test_count() {
    local token="${AGEA_API_TOKEN:-}"
    [ -z "$token" ] && { echo "skip"; return; }
    docker exec docker-bot-1 curl -s --max-time 15 \
        -H "Authorization: Bearer $token" \
        'http://localhost:8000/api/lexia/search?q=photovoltaique' 2>/dev/null \
        | jq -r '.count // "err"' 2>/dev/null || echo "err"
}

check_agea_bot() {
    local count
    count=$(lexia_test_count)

    if [ "$count" = "skip" ]; then
        log "AGEA_API_TOKEN non defini, skip test fonctionnel LEXIA"
        if ! container_running "docker-bot-1"; then
            log "Bot container DOWN, start..."
            (cd "$AGEA_DIR/docker" && docker compose up -d bot >> "$LOG_FILE" 2>&1)
            sleep 20
            if ! container_running "docker-bot-1"; then
                ALERTS_PENDING+=("bot::Container docker-bot-1 impossible à démarrer")
                return 1
            fi
            log "Bot RECOVERED"
        fi
        clear_alert "bot"
        return 0
    fi

    if [ "$count" != "0" ] && [ "$count" != "err" ] && [ -n "$count" ]; then
        clear_alert "bot"
        return 0
    fi

    # Palier 1: restart
    log "LEXIA not functional (count=$count), palier 1: restart bot..."
    (cd "$AGEA_DIR/docker" && docker compose restart bot >> "$LOG_FILE" 2>&1)
    sleep 25
    count=$(lexia_test_count)
    if [ "$count" != "0" ] && [ "$count" != "err" ] && [ -n "$count" ]; then
        log "LEXIA RECOVERED after restart"
        clear_alert "bot"
        return 0
    fi

    # Palier 2: force-recreate
    log "Palier 2: force-recreate bot..."
    (cd "$AGEA_DIR/docker" && docker compose up -d --force-recreate bot >> "$LOG_FILE" 2>&1)
    sleep 35
    count=$(lexia_test_count)
    if [ "$count" != "0" ] && [ "$count" != "err" ] && [ -n "$count" ]; then
        log "LEXIA RECOVERED after force-recreate"
        clear_alert "bot"
        return 0
    fi

    # Palier 3: git pull + rebuild
    log "Palier 3: git pull + rebuild bot..."
    (cd "$AGEA_DIR" && git fetch origin main >> "$LOG_FILE" 2>&1 && git reset --hard origin/main >> "$LOG_FILE" 2>&1)
    (cd "$AGEA_DIR/docker" && docker compose build bot >> "$LOG_FILE" 2>&1 && docker compose up -d bot >> "$LOG_FILE" 2>&1)
    sleep 45
    count=$(lexia_test_count)
    if [ "$count" != "0" ] && [ "$count" != "err" ] && [ -n "$count" ]; then
        log "LEXIA RECOVERED after rebuild"
        clear_alert "bot"
        return 0
    fi

    log "LEXIA STILL BROKEN after 3 paliers (count=$count)"
    ALERTS_PENDING+=("bot::AGEA/LEXIA en panne, auto-réparation échouée (restart+recreate+rebuild). Intervention manuelle requise.")
    return 1
}

check_mcp_remote() {
    if container_running "docker-mcp-remote-1"; then
        local status
        status=$(docker inspect -f '{{.State.Health.Status}}' docker-mcp-remote-1 2>/dev/null || echo "none")
        if [ "$status" = "unhealthy" ]; then
            log "mcp-remote unhealthy, restart..."
            (cd "$AGEA_DIR/docker" && docker compose restart mcp-remote >> "$LOG_FILE" 2>&1)
            sleep 25
            status=$(docker inspect -f '{{.State.Health.Status}}' docker-mcp-remote-1 2>/dev/null || echo "none")
            if [ "$status" = "unhealthy" ]; then
                ALERTS_PENDING+=("mcp::docker-mcp-remote-1 unhealthy après restart")
                return 1
            fi
        fi
        clear_alert "mcp"
        return 0
    fi
    log "mcp-remote DOWN, start..."
    (cd "$AGEA_DIR/docker" && docker compose up -d mcp-remote >> "$LOG_FILE" 2>&1)
    sleep 20
    if container_running "docker-mcp-remote-1"; then
        clear_alert "mcp"
        return 0
    fi
    ALERTS_PENDING+=("mcp::docker-mcp-remote-1 impossible à démarrer")
    return 1
}

check_caddy() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$HEALTH_URL_EXTERNAL" 2>/dev/null || echo 000)
    if [ "$code" != "000" ] && [ "$code" != "502" ] && [ "$code" != "503" ] && [ "$code" != "504" ]; then
        clear_alert "caddy"
        return 0
    fi
    log "Caddy unhealthy (HTTP $code), restart..."
    (cd "$AGEA_DIR/docker" && docker compose restart caddy >> "$LOG_FILE" 2>&1)
    sleep 18
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$HEALTH_URL_EXTERNAL" 2>/dev/null || echo 000)
    if [ "$code" != "000" ] && [ "$code" != "502" ] && [ "$code" != "503" ] && [ "$code" != "504" ]; then
        log "Caddy RECOVERED (HTTP $code)"
        clear_alert "caddy"
        return 0
    fi
    log "Caddy STILL DOWN (HTTP $code)"
    ALERTS_PENDING+=("caddy::Reverse-proxy HTTPS Caddy KO (HTTP $code) après restart — site externe inaccessible")
    return 1
}

check_n8n() {
    if ! container_running "n8n-n8n-1"; then
        log "n8n DOWN, start..."
        (cd "$N8N_DIR" && docker compose up -d >> "$LOG_FILE" 2>&1)
        sleep 20
    fi
    local status
    status=$(docker exec n8n-n8n-1 wget -qO- --timeout=6 http://localhost:5678/healthz 2>/dev/null | jq -r '.status // "ko"' 2>/dev/null || echo "ko")
    if [ "$status" = "ok" ]; then
        clear_alert "n8n"
        return 0
    fi
    log "n8n not ok (status=$status), restart..."
    (cd "$N8N_DIR" && docker compose restart >> "$LOG_FILE" 2>&1)
    sleep 25
    status=$(docker exec n8n-n8n-1 wget -qO- --timeout=6 http://localhost:5678/healthz 2>/dev/null | jq -r '.status // "ko"' 2>/dev/null || echo "ko")
    if [ "$status" = "ok" ]; then
        log "n8n RECOVERED"
        clear_alert "n8n"
        return 0
    fi
    log "n8n STILL DOWN"
    ALERTS_PENDING+=("n8n::n8n non-répondant après restart — workflows arrêtés")
    return 1
}

check_invoiceninja() {
    local any_down=0
    for container in invoiceninja-app invoiceninja-nginx invoiceninja-db; do
        if container_running "$container"; then continue; fi
        log "$container DOWN, start..."
        (cd "$INVOICENINJA_DIR" && docker compose up -d >> "$LOG_FILE" 2>&1)
        sleep 15
        if ! container_running "$container"; then
            any_down=1
            break
        fi
    done
    if [ "$any_down" = "0" ]; then
        clear_alert "invoiceninja"
        return 0
    fi
    ALERTS_PENDING+=("invoiceninja::Un ou plusieurs containers Invoice Ninja KO après restart")
    return 1
}

check_backups() {
    local latest
    latest=$(find "$AGEA_DIR/backups/" -name "*neo4j*.gz" -mtime -2 2>/dev/null | head -1)
    if [ -n "$latest" ]; then
        clear_alert "backup_neo4j"
        return 0
    fi
    ALERTS_PENDING+=("backup_neo4j::Aucun backup Neo4j récent (< 48h) — vérifier cron 4h du matin")
    return 1
}

check_disk() {
    local usage
    usage=$(df / | awk 'NR==2 {gsub("%",""); print $5}')
    if [ "$usage" -gt 85 ]; then
        log "Disk at $usage%, auto-cleanup..."
        find "$AGEA_DIR/backups/" -name "*.gz" -mtime +30 -delete 2>/dev/null || true
        find "$AGEA_DIR/sauvegardes-contexte/" -maxdepth 2 -type d -mtime +60 -exec rm -rf {} + 2>/dev/null || true
        docker system prune -f > /dev/null 2>&1 || true
        usage=$(df / | awk 'NR==2 {gsub("%",""); print $5}')
        log "After cleanup: disk at $usage%"
    fi
    if [ "$usage" -gt 95 ]; then
        ALERTS_PENDING+=("disk::Disque racine à ${usage}% malgré nettoyage auto — intervention requise")
        return 1
    fi
    clear_alert "disk"
    return 0
}

# === RUN ===

log "=== Run start ==="

check_postgres || true
check_neo4j || true
check_agea_bot || true
check_mcp_remote || true
check_caddy || true
check_n8n || true
check_invoiceninja || true
check_backups || true
check_disk || true

# === ALERTING ===

if [ "$TEST_MODE" = "true" ]; then
    status_line="✅ Tout OK"
    [ ${#ALERTS_PENDING[@]} -gt 0 ] && status_line="⚠️ ${#ALERTS_PENDING[@]} problème(s) détecté(s)"
    test_msg="🔧 <b>AGEA self-heal --test</b>%0A$status_line%0A$(date -u '+%Y-%m-%d %H:%M UTC')%0APipeline Telegram OK."
    telegram_send "$(printf '%b' "$test_msg" | sed 's/%0A/\n/g')"
    log "TEST mode: message Telegram envoyé"
fi

if [ ${#RECOVERED[@]} -gt 0 ]; then
    recovered_msg="✅ <b>AGEA self-heal — résolu</b>%0A$(date -u '+%Y-%m-%d %H:%M UTC')%0A%0A"
    for item in "${RECOVERED[@]}"; do
        recovered_msg+="• $item rétabli%0A"
    done
    telegram_send "$(printf '%b' "$recovered_msg" | sed 's/%0A/\n/g')"
    log "RECOVERED: ${RECOVERED[*]}"
fi

if [ ${#ALERTS_PENDING[@]} -gt 0 ]; then
    to_send=()
    for entry in "${ALERTS_PENDING[@]}"; do
        key="${entry%%::*}"
        detail="${entry#*::}"
        if should_alert "$key"; then
            to_send+=("• $detail")
            mark_alerted "$key"
        else
            log "Alert $key en cooldown, pas ré-envoyée"
        fi
    done
    if [ ${#to_send[@]} -gt 0 ]; then
        alert_msg="🚨 <b>AGEA self-heal — panne</b>%0A$(date -u '+%Y-%m-%d %H:%M UTC')%0A%0A"
        for line in "${to_send[@]}"; do
            alert_msg+="$line%0A"
        done
        alert_msg+="%0AAuto-réparation échouée. Intervention manuelle requise."
        telegram_send "$(printf '%b' "$alert_msg" | sed 's/%0A/\n/g')"
        log "ALERT sent: ${#to_send[@]} item(s)"
    fi
fi

log "=== Run end (pending=${#ALERTS_PENDING[@]} recovered=${#RECOVERED[@]}) ==="
exit 0
