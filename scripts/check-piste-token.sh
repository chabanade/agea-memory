#!/usr/bin/env bash
# AGEA PISTE Health Check
# Verifie periodiquement la sante du credential PISTE/DILA (Legifrance).
# Alerte Telegram en cas de probleme stable (secret expire, quota, maintenance).
# Le client Python gere deja l'auto-renewal du token a la volee via _get_token().
#
# Usage :
#   check-piste-token.sh          # silence si OK, alerte Telegram si KO
#   check-piste-token.sh --test   # force un message Telegram meme en cas de succes
#
# Exit codes :
#   0 = OK
#   1 = echec obtention token (credential probablement invalide)
#   2 = token OK mais API Legifrance KO (quota ou maintenance)
#   3 = erreur interne (dependance manquante, .env illisible, etc.)

set -euo pipefail

ENV_FILE="/opt/agea/.env"
SCRIPT_NAME="[AGEA PISTE Health]"
TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"

TEST_MODE=0
if [[ "${1:-}" == "--test" ]]; then
    TEST_MODE=1
fi

log_stdout() {
    echo "$TS $SCRIPT_NAME $*"
}

log_stderr() {
    echo "$TS $SCRIPT_NAME $*" >&2
}

# Envoi Telegram. Ne fuite jamais les secrets.
# Arg 1 : message (texte brut, sera transmis tel quel).
send_telegram() {
    local msg="$1"
    local full_msg="$SCRIPT_NAME $msg"
    # Les alertes vont aussi vers stderr (visible dans le log cron)
    log_stderr "ALERT -> Telegram: $msg"

    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${MEHDI_CHAT_ID:-}" ]]; then
        log_stderr "Telegram non configure (TELEGRAM_BOT_TOKEN ou MEHDI_CHAT_ID manquant)."
        return 1
    fi

    # -s : silencieux ; --data-urlencode : encodage correct ; pas de log du token
    local response
    response="$(curl -s --max-time 10 -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${MEHDI_CHAT_ID}" \
        --data-urlencode "text=${full_msg}" \
        --data-urlencode "disable_web_page_preview=true" \
        2>/dev/null || true)"

    # On log uniquement le champ ok (pas de secrets dans la reponse sendMessage)
    local ok
    ok="$(echo "$response" | jq -r '.ok // "unknown"' 2>/dev/null || echo "unknown")"
    log_stderr "Telegram API ok=$ok"
    if [[ "$ok" != "true" ]]; then
        return 1
    fi
    return 0
}

# ---------- 0. Pre-flight ----------
if ! command -v jq >/dev/null 2>&1; then
    log_stderr "Dependance manquante : jq"
    exit 3
fi
if ! command -v curl >/dev/null 2>&1; then
    log_stderr "Dependance manquante : curl"
    exit 3
fi

if [[ ! -r "$ENV_FILE" ]]; then
    log_stderr "Fichier .env illisible : $ENV_FILE"
    exit 3
fi

# ---------- 1. Chargement .env ----------
# shellcheck disable=SC1090
set -a
. "$ENV_FILE"
set +a

PISTE_TOKEN_URL="${PISTE_TOKEN_URL:-https://oauth.piste.gouv.fr/api/oauth/token}"
LEGIFRANCE_API_URL="${LEGIFRANCE_API_URL:-https://api.piste.gouv.fr/dila/legifrance/lf-engine-app}"

if [[ -z "${PISTE_CLIENT_ID:-}" || -z "${PISTE_CLIENT_SECRET:-}" ]]; then
    msg="PISTE_CLIENT_ID ou PISTE_CLIENT_SECRET manquant dans .env. Action requise : verifier /opt/agea/.env."
    send_telegram "$msg" || true
    log_stderr "$msg"
    exit 1
fi

# ---------- 2. Obtention token ----------
# On stocke la reponse dans un fichier temporaire pour ne pas melanger body + http_code
TMP_BODY="$(mktemp)"
trap 'rm -f "$TMP_BODY"' EXIT

HTTP_CODE="$(curl -s --max-time 15 -o "$TMP_BODY" -w "%{http_code}" \
    -X POST "$PISTE_TOKEN_URL" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "grant_type=client_credentials" \
    --data-urlencode "client_id=${PISTE_CLIENT_ID}" \
    --data-urlencode "client_secret=${PISTE_CLIENT_SECRET}" \
    --data-urlencode "scope=openid" \
    || echo "000")"

ACCESS_TOKEN="$(jq -r '.access_token // empty' < "$TMP_BODY" 2>/dev/null || true)"

if [[ "$HTTP_CODE" != "200" || -z "$ACCESS_TOKEN" ]]; then
    # On n'ecrit PAS le body brut (peut contenir des details exploitables)
    err_desc="$(jq -r '.error // .error_description // "inconnu"' < "$TMP_BODY" 2>/dev/null || echo "inconnu")"
    msg="Echec obtention token PISTE (HTTP $HTTP_CODE, erreur=$err_desc). Action requise : verifier PISTE_CLIENT_SECRET dans /opt/agea/.env (regeneration possible sur piste.gouv.fr)."
    send_telegram "$msg" || true
    log_stderr "$msg"
    exit 1
fi

log_stdout "Token PISTE obtenu (HTTP $HTTP_CODE)."

# ---------- 3. Test endpoint /search Legifrance ----------
SEARCH_PAYLOAD="$(jq -n '{
  recherche: {
    champs: [
      {
        typeChamp: "ALL",
        criteres: [
          { typeRecherche: "EXACTE", valeur: "photovoltaique", operateur: "ET" }
        ],
        operateur: "ET"
      }
    ],
    filtres: [],
    pageNumber: 1,
    pageSize: 1,
    operateur: "ET",
    sort: "PERTINENCE",
    typePagination: "DEFAUT"
  },
  fond: "CODE_DATE"
}')"

TMP_SEARCH="$(mktemp)"
trap 'rm -f "$TMP_BODY" "$TMP_SEARCH"' EXIT

SEARCH_HTTP="$(curl -s --max-time 20 -o "$TMP_SEARCH" -w "%{http_code}" \
    -X POST "${LEGIFRANCE_API_URL%/}/search" \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    --data "$SEARCH_PAYLOAD" \
    || echo "000")"

if [[ "$SEARCH_HTTP" != "200" ]]; then
    msg="PISTE token OK mais API Legifrance renvoie HTTP $SEARCH_HTTP. Verifier quota PISTE (500 req/jour) ou maintenance DILA (https://status.piste.gouv.fr)."
    send_telegram "$msg" || true
    log_stderr "$msg"
    exit 2
fi

log_stdout "OK - PISTE token valide, Legifrance /search HTTP $SEARCH_HTTP."

# ---------- 4. Mode --test : on force un message Telegram meme si tout va bien ----------
if [[ "$TEST_MODE" -eq 1 ]]; then
    msg="Test OK - token PISTE valide, Legifrance /search HTTP 200. Pipeline Telegram fonctionnel."
    if send_telegram "$msg"; then
        log_stdout "Message Telegram de test envoye avec succes."
    else
        log_stderr "Echec envoi Telegram de test."
        exit 3
    fi
fi

exit 0
