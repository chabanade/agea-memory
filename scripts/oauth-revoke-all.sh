#!/usr/bin/env bash
# Revocation d'urgence : deconnecte tous les clients OAuth en invalidant tous les tokens.
# A utiliser en cas de fuite suspecte ou d'incident securite.
# Impact : les clients OAuth (ChatGPT Business, etc.) devront refaire le flow complet.
# N'affecte PAS Claude Desktop / Code / scripts / cron (qui passent par /mcp Bearer direct).
set -euo pipefail

CONTAINER="${1:-docker-oauth-proxy-1}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/data/oauth.db.revoke-${STAMP}.bak"

echo "[oauth-revoke-all ${STAMP}] Backup DB avant revocation : ${BACKUP}"
docker exec "$CONTAINER" sqlite3 /data/oauth.db ".backup '${BACKUP}'"

echo "[oauth-revoke-all ${STAMP}] Revocation de tous les tokens actifs"
REVOKED=$(docker exec "$CONTAINER" sqlite3 /data/oauth.db \
  "UPDATE tokens SET revoked=1 WHERE revoked=0; SELECT changes();")
echo "[oauth-revoke-all ${STAMP}] Tokens revoques : ${REVOKED}"

echo "[oauth-revoke-all ${STAMP}] Revocation des auth_codes non consommes"
CODES=$(docker exec "$CONTAINER" sqlite3 /data/oauth.db \
  "UPDATE auth_codes SET consumed=1 WHERE consumed=0; SELECT changes();")
echo "[oauth-revoke-all ${STAMP}] Auth codes consommes : ${CODES}"

echo "[oauth-revoke-all ${STAMP}] Termine. Audit log : docker exec ${CONTAINER} cat /data/audit.log | tail"
