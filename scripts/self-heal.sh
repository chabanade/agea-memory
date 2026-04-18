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
REBUILD_COOLDOWN=14400  # 4h - circuit breaker palier 3 bot (évite boucle rebuild)
SERVICE_RESTART_COOLDOWN=14400  # 4h - circuit breaker services non-bot (postgres/neo4j/caddy/n8n/invoiceninja)
LEXIA_EXTERNAL_PERSIST_OUVRE=2   # runs consecutifs avant alerte PISTE en heures ouvrees (~1h)
LEXIA_EXTERNAL_PERSIST_REPOS=6   # runs consecutifs avant alerte PISTE hors heures ouvrees (~3h)
MCP_RESTART_GRACE=60  # tolerance (s) entre horodatage "restart autorise" et StartedAt reel
HEALTH_URL_EXTERNAL="https://srv987452.hstgr.cloud"
LOG_PREFIX="[AGEA self-heal]"

# Patterns indiquant une corruption BDD — ne PAS restart si detecte
DB_CORRUPTION_PATTERN='(corrupt|panic|fatal.*disk|data.*loss|unrecoverable|cannot read block|segmentation fault|database.*is.*inconsistent)'

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

# Heure ouvree = lun-ven 8h-20h, fuseau Europe/Paris (gere DST automatiquement)
is_heure_ouvree() {
    local h j
    h=$(TZ=Europe/Paris date +%H)
    j=$(TZ=Europe/Paris date +%u)  # 1=lun, 7=dim
    [ "$j" -le 5 ] && [ "$h" -ge 8 ] && [ "$h" -lt 20 ]
}

# Circuit breaker generique par service (4h). Retourne 0 si restart autorise.
service_restart_allowed() {
    local svc=$1
    local last now
    last=$(jq -r --arg s "$svc" '.last_restart[$s] // 0' "$STATE_FILE" 2>/dev/null || echo 0)
    now=$(date +%s)
    [ $((now - last)) -gt "$SERVICE_RESTART_COOLDOWN" ]
}

mark_service_restart() {
    local svc=$1
    local now tmp
    now=$(date +%s)
    tmp=$(mktemp)
    jq --arg s "$svc" --argjson t "$now" '.last_restart[$s] = $t | .last_restart |= (. // {})' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
}

# Verifie que les logs recents d'un container BDD ne crient pas corruption/fatal disk.
# Retourne 0 si logs sains (restart OK), 1 si corruption detectee (NE PAS restart).
db_logs_safe() {
    local container=$1
    if ! container_exists "$container"; then
        return 0  # pas de logs a verifier, laisser passer
    fi
    if docker logs --tail 50 "$container" 2>&1 | grep -iqE "$DB_CORRUPTION_PATTERN"; then
        return 1
    fi
    return 0
}

# bot_alive() : le bot repond a /health ET /api/facts (DB joignable).
# Aucune dependance PISTE. Definition canonique de "bot vivant".
bot_alive() {
    docker exec docker-bot-1 curl -sf --max-time 8 http://localhost:8000/health > /dev/null 2>&1 || return 1
    local token="${AGEA_API_TOKEN:-}"
    [ -z "$token" ] && return 0  # si token absent, /health suffit
    docker exec docker-bot-1 curl -sf --max-time 10 \
        -H "Authorization: Bearer $token" \
        'http://localhost:8000/api/facts?q=test&limit=1' > /dev/null 2>&1
}

# Marque un restart MCP comme "autorise" par le self-heal (pour le watchdog).
mark_mcp_restart_authorized() {
    local now tmp
    now=$(date +%s)
    tmp=$(mktemp)
    jq --argjson t "$now" '.last_mcp_restart_authorized = $t' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
}

# === CHECKS ===

check_postgres() {
    # Politique : container stopped -> restart auto. Container running mais KO -> alerte (pas de restart risqué).
    if ! container_running "docker-postgres-1"; then
        if ! service_restart_allowed "postgres"; then
            log "Postgres stopped mais restart deja tente il y a <4h, skip (circuit breaker)"
            ALERTS_PENDING+=("postgres::Postgres stoppe et deja redemarre il y a <4h — instabilite persistante, intervention manuelle.")
            return 1
        fi
        if ! db_logs_safe "docker-postgres-1"; then
            log "Postgres logs contiennent pattern corruption, PAS de restart auto"
            ALERTS_PENDING+=("postgres::Logs postgres contiennent pattern 'corruption/fatal/panic'. PAS de restart auto pour eviter aggravation. Intervention manuelle.")
            return 1
        fi
        log "Postgres stopped, start up -d --no-deps..."
        (cd "$AGEA_DIR/docker" && docker compose up -d --no-deps postgres >> "$LOG_FILE" 2>&1)
        mark_service_restart "postgres"
        sleep 20
        if docker exec docker-postgres-1 pg_isready -q 2>/dev/null; then
            log "Postgres RECOVERED after start"
            clear_alert "postgres"
            return 0
        fi
        ALERTS_PENDING+=("postgres::Postgres impossible a demarrer apres up -d")
        return 1
    fi
    # Container running — juste un test sans restart
    if docker exec docker-postgres-1 pg_isready -q 2>/dev/null; then
        clear_alert "postgres"
        return 0
    fi
    log "Postgres running mais pg_isready KO — PAS de restart auto (risque transactions en cours)"
    ALERTS_PENDING+=("postgres::Container postgres running mais ne repond plus a pg_isready. PAS de restart auto (protege transactions). Intervention manuelle.")
    return 1
}

check_neo4j() {
    local pwd_neo4j="${NEO4J_PASSWORD:-}"
    if [ -z "$pwd_neo4j" ]; then
        log "NEO4J_PASSWORD vide, skip neo4j check"
        return 0
    fi
    # Politique : stopped -> restart auto. Running KO -> alerte, pas de restart.
    if ! container_running "docker-neo4j-1"; then
        if ! service_restart_allowed "neo4j"; then
            log "Neo4j stopped mais restart deja tente il y a <4h, skip (circuit breaker)"
            ALERTS_PENDING+=("neo4j::Neo4j stoppe et deja redemarre il y a <4h — instabilite persistante.")
            return 1
        fi
        if ! db_logs_safe "docker-neo4j-1"; then
            log "Neo4j logs contiennent pattern corruption, PAS de restart auto"
            ALERTS_PENDING+=("neo4j::Logs neo4j contiennent pattern 'corruption/fatal'. PAS de restart auto. Intervention manuelle.")
            return 1
        fi
        log "Neo4j stopped, start up -d --no-deps..."
        (cd "$AGEA_DIR/docker" && docker compose up -d --no-deps neo4j >> "$LOG_FILE" 2>&1)
        mark_service_restart "neo4j"
        sleep 35
        if docker exec docker-neo4j-1 cypher-shell -u neo4j -p "$pwd_neo4j" "RETURN 1" > /dev/null 2>&1; then
            log "Neo4j RECOVERED after start"
            clear_alert "neo4j"
            return 0
        fi
        ALERTS_PENDING+=("neo4j::Neo4j impossible a demarrer apres up -d")
        return 1
    fi
    if docker exec docker-neo4j-1 cypher-shell -u neo4j -p "$pwd_neo4j" "RETURN 1" > /dev/null 2>&1; then
        clear_alert "neo4j"
        return 0
    fi
    log "Neo4j running mais cypher-shell KO — PAS de restart auto (risque transactions Graphiti)"
    ALERTS_PENDING+=("neo4j::Container neo4j running mais ne repond plus a cypher-shell. PAS de restart auto. Intervention manuelle.")
    return 1
}

# Retourne skip | ok | external | internal | dead
# - ok        : LEXIA fonctionne normalement
# - external  : panne cote PISTE/Legifrance (HTTP 5XX, timeout, 429 quota, body vide API-relai) → PAS de reparation
# - internal  : le bot repond mais JSON invalide / code inattendu
# - dead      : le bot ne repond pas du tout (reparation bot necessaire)
# - skip      : pas de token AGEA_API_TOKEN configure
lexia_check() {
    local token="${AGEA_API_TOKEN:-}"
    [ -z "$token" ] && { echo "skip"; return; }

    # Bot injoignable -> dead (c'est un probleme de liveness, pas PISTE)
    bot_health_alive || { echo "dead"; return; }

    # Recupere HTTP code + body en une requete (probe ecrit dans /tmp du container)
    local http_code response
    http_code=$(docker exec docker-bot-1 curl -s -o /tmp/lexia_probe.json -w '%{http_code}' --max-time 15 \
        -H "Authorization: Bearer $token" \
        'http://localhost:8000/api/lexia/search?q=photovoltaique' 2>/dev/null || echo 000)
    response=$(docker exec docker-bot-1 cat /tmp/lexia_probe.json 2>/dev/null || echo "")

    # Timeout / pas de reponse -> bot ou reseau entre bot et PISTE
    if [ "$http_code" = "000" ]; then
        echo "dead"
        return
    fi

    # Body vide mais HTTP 200 = souvent relai PISTE qui a coupe
    if [ -z "$response" ]; then
        if [ "$http_code" = "200" ]; then
            echo "external"
            return
        fi
        echo "dead"
        return
    fi

    # HTTP 5XX cote bot = erreur bot non gerée (pas un probleme PISTE upstream)
    if echo "$http_code" | grep -qE '^5'; then
        echo "internal"
        return
    fi

    # 429 cote bot = quota PISTE
    if [ "$http_code" = "429" ]; then
        echo "external"
        return
    fi

    # Reponse non-JSON valide
    if ! echo "$response" | jq -e . > /dev/null 2>&1; then
        echo "internal"
        return
    fi

    # Le bot a repondu en JSON. Verifier si la reponse signale une erreur externe PISTE.
    if echo "$response" | jq -e '.results[0].source == "error"' > /dev/null 2>&1; then
        local errmsg
        errmsg=$(echo "$response" | jq -r '.results[0].title // ""')
        if echo "$errmsg" | grep -qiE 'Legifrance API HTTP 5|HTTP 50[234]|timeout|temporairement indisponible|quota|429'; then
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

# Check #1: le bot est-il vivant ? Utilise bot_alive() = /health + /api/facts.
# Independant de PISTE. Si KO, enchaine paliers 1/2/3 en ciblant UNIQUEMENT le service bot (--no-deps).
check_bot_liveness() {
    if ! container_running "docker-bot-1"; then
        log "Bot container DOWN, start..."
        (cd "$AGEA_DIR/docker" && docker compose up -d --no-deps bot >> "$LOG_FILE" 2>&1)
        sleep 20
    fi

    if bot_alive; then
        clear_alert "bot"
        return 0
    fi

    log "Bot KO (pas de reponse /health ou /api/facts), palier 1: restart bot..."
    (cd "$AGEA_DIR/docker" && docker compose restart bot >> "$LOG_FILE" 2>&1)
    sleep 25
    if bot_alive; then
        log "Bot RECOVERED after restart"
        clear_alert "bot"
        return 0
    fi

    log "Palier 2: force-recreate bot (--no-deps, ne touche pas mcp-remote/neo4j)..."
    (cd "$AGEA_DIR/docker" && docker compose up -d --no-deps --force-recreate bot >> "$LOG_FILE" 2>&1)
    sleep 35
    if bot_alive; then
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
    if bot_alive; then
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
            local fails tmp seuil
            fails=$(jq -r '.lexia_external_fails // 0' "$STATE_FILE" 2>/dev/null || echo 0)
            fails=$((fails + 1))
            tmp=$(mktemp)
            jq --argjson f "$fails" '.lexia_external_fails = $f' "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
            # Option B : 1h en heures ouvrees (seuil=2 runs), 3h hors (seuil=6 runs)
            if is_heure_ouvree; then
                seuil="$LEXIA_EXTERNAL_PERSIST_OUVRE"
            else
                seuil="$LEXIA_EXTERNAL_PERSIST_REPOS"
            fi
            log "LEXIA external error (PISTE/Legifrance), fails=$fails seuil=$seuil, pas de reparation locale"
            if [ "$fails" -ge "$seuil" ]; then
                ALERTS_PENDING+=("lexia_external::API Legifrance/PISTE en panne persistante (seuil $seuil runs atteint). Rien a faire cote AGEA, attendre retablissement DILA.")
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
        mark_mcp_restart_authorized
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
    mark_mcp_restart_authorized
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

# Watchdog : detecte un restart de mcp-remote non declenche par le self-heal.
# Utile si un autre script ou un redeploiement relance mcp-remote a notre insu
# (ex: ce qui s'est passe le 19/04/2026 06:02 : up -d global a relance mcp-remote,
# cassant la session MCP client).
mcp_uptime_watchdog() {
    container_running "docker-mcp-remote-1" || return 0

    local started_at_iso started_at last_auth now delta
    started_at_iso=$(docker inspect -f '{{.State.StartedAt}}' docker-mcp-remote-1 2>/dev/null)
    [ -z "$started_at_iso" ] && return 0
    started_at=$(date -d "$started_at_iso" +%s 2>/dev/null || echo 0)
    [ "$started_at" = "0" ] && return 0

    last_auth=$(jq -r '.last_mcp_restart_authorized // 0' "$STATE_FILE" 2>/dev/null || echo 0)
    now=$(date +%s)
    delta=$((now - started_at))

    # Si demarre dans les 10 dernieres min ET sans autorisation self-heal -> alerte
    if [ "$delta" -lt 600 ]; then
        if [ "$started_at" -gt $((last_auth + MCP_RESTART_GRACE)) ]; then
            log "mcp-remote redemarre il y a ${delta}s SANS autorisation self-heal (last_auth=$last_auth, started_at=$started_at)"
            ALERTS_PENDING+=("mcp_watchdog::mcp-remote a redemarre il y a ${delta}s sans ordre du self-heal. Sessions MCP clients probablement cassees. Verifier ce qui a touche au container (autre script, redeploiement manuel, OOM...).")
        fi
    fi
    return 0
}

check_caddy() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$HEALTH_URL_EXTERNAL" 2>/dev/null || echo 000)
    if [ "$code" != "000" ] && [ "$code" != "502" ] && [ "$code" != "503" ] && [ "$code" != "504" ]; then
        clear_alert "caddy"
        return 0
    fi
    # Caddy en panne. Si container stopped -> restart, sinon alerte.
    if ! container_running "docker-caddy-1"; then
        if ! service_restart_allowed "caddy"; then
            log "Caddy stopped mais deja redemarre <4h (circuit breaker)"
            ALERTS_PENDING+=("caddy::Caddy stoppe et deja redemarre <4h")
            return 1
        fi
        log "Caddy stopped, start up -d --no-deps..."
        (cd "$AGEA_DIR/docker" && docker compose up -d --no-deps caddy >> "$LOG_FILE" 2>&1)
        mark_service_restart "caddy"
        sleep 18
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$HEALTH_URL_EXTERNAL" 2>/dev/null || echo 000)
        if [ "$code" != "000" ] && [ "$code" != "502" ] && [ "$code" != "503" ] && [ "$code" != "504" ]; then
            log "Caddy RECOVERED apres start"
            clear_alert "caddy"
            return 0
        fi
        ALERTS_PENDING+=("caddy::Caddy impossible a demarrer (HTTP $code)")
        return 1
    fi
    log "Caddy running mais HTTPS KO (HTTP $code) — PAS de restart auto (risque certificats/upstream config)"
    ALERTS_PENDING+=("caddy::Container caddy running mais HTTPS externe repond $code. PAS de restart auto (possible upstream down). Intervention manuelle.")
    return 1
}

check_n8n() {
    # Politique : stopped -> restart auto (1 seul palier). Running KO -> alerte.
    if ! container_running "n8n-n8n-1"; then
        if ! service_restart_allowed "n8n"; then
            log "n8n stopped mais deja redemarre <4h (circuit breaker)"
            ALERTS_PENDING+=("n8n::n8n stoppe et deja redemarre <4h")
            return 1
        fi
        log "n8n stopped, start..."
        (cd "$N8N_DIR" && docker compose up -d --no-deps n8n >> "$LOG_FILE" 2>&1)
        mark_service_restart "n8n"
        sleep 25
        local status
        status=$(docker exec n8n-n8n-1 wget -qO- --timeout=6 http://localhost:5678/healthz 2>/dev/null | jq -r '.status // "ko"' 2>/dev/null || echo "ko")
        if [ "$status" = "ok" ]; then
            log "n8n RECOVERED apres start"
            clear_alert "n8n"
            return 0
        fi
        ALERTS_PENDING+=("n8n::n8n impossible a demarrer apres up -d")
        return 1
    fi
    local status
    status=$(docker exec n8n-n8n-1 wget -qO- --timeout=6 http://localhost:5678/healthz 2>/dev/null | jq -r '.status // "ko"' 2>/dev/null || echo "ko")
    if [ "$status" = "ok" ]; then
        clear_alert "n8n"
        return 0
    fi
    log "n8n running mais healthz=$status — PAS de restart auto (possible workflow en cours)"
    ALERTS_PENDING+=("n8n::Container n8n running mais healthz=$status. PAS de restart auto (protege workflows). Intervention manuelle.")
    return 1
}

check_invoiceninja() {
    # Politique : stopped -> un seul up -d (pas de restart si running). Pas de re-up si cooldown actif.
    local stopped=()
    for container in invoiceninja-app invoiceninja-nginx invoiceninja-db; do
        container_running "$container" || stopped+=("$container")
    done

    if [ "${#stopped[@]}" -eq 0 ]; then
        clear_alert "invoiceninja"
        return 0
    fi

    if ! service_restart_allowed "invoiceninja"; then
        log "Invoice Ninja down mais deja redemarre <4h (circuit breaker)"
        ALERTS_PENDING+=("invoiceninja::Invoice Ninja containers stopped et deja redemarres <4h : ${stopped[*]}")
        return 1
    fi

    # Corruption DB check avant restart invoiceninja-db
    for c in "${stopped[@]}"; do
        if [ "$c" = "invoiceninja-db" ] && ! db_logs_safe "invoiceninja-db"; then
            log "invoiceninja-db logs contiennent pattern corruption, PAS de restart auto"
            ALERTS_PENDING+=("invoiceninja::invoiceninja-db logs corruption/fatal. PAS de restart auto. Intervention manuelle.")
            return 1
        fi
    done

    log "Invoice Ninja: start containers ${stopped[*]}..."
    (cd "$INVOICENINJA_DIR" && docker compose up -d >> "$LOG_FILE" 2>&1)
    mark_service_restart "invoiceninja"
    sleep 20

    local still_down=()
    for c in "${stopped[@]}"; do
        container_running "$c" || still_down+=("$c")
    done
    if [ "${#still_down[@]}" -eq 0 ]; then
        log "Invoice Ninja RECOVERED"
        clear_alert "invoiceninja"
        return 0
    fi
    ALERTS_PENDING+=("invoiceninja::Invoice Ninja impossible a demarrer : ${still_down[*]}")
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
mcp_uptime_watchdog || true
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
