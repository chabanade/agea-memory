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
REBUILD_COOLDOWN=14400  # 4h - circuit breaker palier 3 (évite boucle rebuild)
LEXIA_EXTERNAL_PERSIST=6  # runs consécutifs avant d'alerter sur panne PISTE
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

# Retourne skip | ok | external | internal | dead
# - ok        : LEXIA fonctionne normalement
# - external  : erreur PISTE/Legifrance (HTTP 5XX cote gouvernement) → PAS de reparation locale
# - internal  : le bot repond mais retourne une erreur (code, format)
# - dead      : le bot ne repond pas du tout
# - skip      : pas de token AGEA_API_TOKEN configure
lexia_check() {
    local token="${AGEA_API_TOKEN:-}"
    [ -z "$token" ] && { echo "skip"; return; }

    local response
    response=$(docker exec docker-bot-1 curl -s --max-time 15 \
        -H "Authorization: Bearer $token" \
        'http://localhost:8000/api/lexia/search?q=photovoltaique' 2>/dev/null) || {
        echo "dead"
        return
    }

    [ -z "$response" ] && { echo "dead"; return; }

    # Le bot a repondu. Verifier si la reponse signale une erreur externe PISTE.
    if echo "$response" | jq -e '.results[0].source == "error"' > /dev/null 2>&1; then
        local errmsg
        errmsg=$(echo "$response" | jq -r '.results[0].title // ""')
        if echo "$errmsg" | grep -qiE 'Legifrance API HTTP 5|HTTP 50[234]|timeout|temporairement indisponible'; then
            echo "external"
            return
        fi
        echo "internal"
        return
    fi

    local count
    count=$(echo "$response" | jq -r '.count // empty' 2>/dev/null)
    if [ -n "$count" ] && [ "$count" != "null" ]; then
        echo "ok"
        return
    fi

    echo "internal"
}

# Verifie uniquement que le bot est vivant (endpoint /health, independant de PISTE)
bot_health_alive() {
    docker exec docker-bot-1 curl -sf --max-time 8 http://localhost:8000/health > /dev/null 2>&1
}

# Circuit breaker: retourne 0 si on peut rebuild (>4h depuis le dernier), 1 sinon.
rebuild_allowed() {
    local last now
    last=$(jq -r '.last_rebuild // 0' "$STATE_FILE" 2>/dev/null || echo 0)
    now=$(date +%s)
    [ $((now - last)) -gt "$REBUILD_COOLDOWN" ]
}

mark_rebuild() {
    local now tmp
    now=$(date +%s)
    tmp=$(mktemp)
    jq --argjson t "$now" '.last_rebuild = $t' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
}

# Check #1: le bot est-il vivant ? (ne depend PAS de PISTE)
# Si KO, enchaine paliers 1/2/3 en ciblant UNIQUEMENT le service bot (--no-deps).
check_bot_liveness() {
    if ! container_running "docker-bot-1"; then
        log "Bot container DOWN, start..."
        (cd "$AGEA_DIR/docker" && docker compose up -d --no-deps bot >> "$LOG_FILE" 2>&1)
        sleep 20
    fi

    if bot_health_alive; then
        clear_alert "bot"
        return 0
    fi

    log "Bot /health KO, palier 1: restart bot..."
    (cd "$AGEA_DIR/docker" && docker compose restart bot >> "$LOG_FILE" 2>&1)
    sleep 25
    if bot_health_alive; then
        log "Bot RECOVERED after restart"
        clear_alert "bot"
        return 0
    fi

    log "Palier 2: force-recreate bot (--no-deps, ne touche pas mcp-remote/neo4j)..."
    (cd "$AGEA_DIR/docker" && docker compose up -d --no-deps --force-recreate bot >> "$LOG_FILE" 2>&1)
    sleep 35
    if bot_health_alive; then
        log "Bot RECOVERED after force-recreate"
        clear_alert "bot"
        return 0
    fi

    if ! rebuild_allowed; then
        log "Palier 3 SKIP: rebuild effectue il y a moins de 4h (circuit breaker)"
        ALERTS_PENDING+=("bot::Bot AGEA KO apres restart+recreate ; rebuild skippe (circuit breaker 4h). Intervention manuelle requise.")
        return 1
    fi

    log "Palier 3: git pull + rebuild bot (--no-deps)..."
    if (cd "$AGEA_DIR" && git fetch origin main >> "$LOG_FILE" 2>&1); then
        local dirty
        dirty=$(cd "$AGEA_DIR" && git status --porcelain | wc -l)
        if [ "$dirty" -eq 0 ]; then
            (cd "$AGEA_DIR" && git reset --hard origin/main >> "$LOG_FILE" 2>&1)
        else
            log "Palier 3: git dirty ($dirty fichiers), SKIP reset --hard"
        fi
    fi
    (cd "$AGEA_DIR/docker" && docker compose build bot >> "$LOG_FILE" 2>&1 && docker compose up -d --no-deps bot >> "$LOG_FILE" 2>&1)
    mark_rebuild
    sleep 45
    if bot_health_alive; then
        log "Bot RECOVERED after rebuild"
        clear_alert "bot"
        return 0
    fi

    log "Bot STILL DEAD after 3 paliers"
    ALERTS_PENDING+=("bot::AGEA bot KO, auto-reparation echouee (restart+recreate+rebuild). Intervention manuelle requise.")
    return 1
}

# Check #2: LEXIA est-il fonctionnel ? (depend de PISTE)
# Distingue panne externe (PISTE gouvernement) vs panne interne.
# Ne declenche AUCUN redemarrage sur une panne externe — attente du retablissement DILA.
check_lexia_functional() {
    local kind
    kind=$(lexia_check)

    case "$kind" in
        skip)
            log "AGEA_API_TOKEN non defini, skip test fonctionnel LEXIA"
            return 0
            ;;
        ok)
            clear_alert "lexia_external"
            local tmp
            tmp=$(mktemp)
            jq '.lexia_external_fails = 0' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
            return 0
            ;;
        external)
            local fails tmp
            fails=$(jq -r '.lexia_external_fails // 0' "$STATE_FILE" 2>/dev/null || echo 0)
            fails=$((fails + 1))
            tmp=$(mktemp)
            jq --argjson f "$fails" '.lexia_external_fails = $f' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
            log "LEXIA external error (PISTE/Legifrance HTTP 5XX), fails=$fails, pas de reparation locale"
            if [ "$fails" -ge "$LEXIA_EXTERNAL_PERSIST" ]; then
                ALERTS_PENDING+=("lexia_external::API Legifrance/PISTE en panne depuis 3h+ cote gouvernement. Rien a faire cote AGEA, attendre retablissement DILA.")
            fi
            return 0
            ;;
        internal|dead)
            log "LEXIA $kind (probleme bot local) — check_bot_liveness doit s'en charger"
            return 1
            ;;
    esac
}

# Test protocole MCP reel (JSON-RPC tools/list) — pas juste le container.
# Verifie que les clients (Claude Desktop, VSCode) pourront vraiment communiquer.
mcp_protocol_alive() {
    local code
    code=$(docker exec docker-bot-1 curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        -X POST http://mcp-remote:8888/mcp \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null || echo 000)
    [ "$code" = "200" ]
}

check_mcp_deep() {
    if ! container_running "docker-mcp-remote-1"; then
        log "mcp-remote container DOWN, start..."
        (cd "$AGEA_DIR/docker" && docker compose up -d --no-deps mcp-remote >> "$LOG_FILE" 2>&1)
        sleep 20
        if ! container_running "docker-mcp-remote-1"; then
            ALERTS_PENDING+=("mcp::docker-mcp-remote-1 impossible a demarrer")
            return 1
        fi
    fi

    if mcp_protocol_alive; then
        clear_alert "mcp"
        return 0
    fi

    log "mcp-remote ne repond pas au protocole MCP (JSON-RPC), restart..."
    (cd "$AGEA_DIR/docker" && docker compose restart mcp-remote >> "$LOG_FILE" 2>&1)
    sleep 25
    if mcp_protocol_alive; then
        log "mcp-remote RECOVERED (protocole)"
        clear_alert "mcp"
        return 0
    fi

    ALERTS_PENDING+=("mcp::docker-mcp-remote-1 ne repond pas au protocole JSON-RPC apres restart. Sessions MCP clients probablement cassees.")
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
check_bot_liveness || true
check_lexia_functional || true
check_mcp_deep || true
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
