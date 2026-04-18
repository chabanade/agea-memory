# AGEA OAuth 2.1 proxy

Container independant qui ajoute OAuth 2.1 devant le serveur MCP AGEA, sans toucher au code de `mcp-remote-server.py`.

## Endpoints exposes

| Path | Role |
|---|---|
| `/healthz` + `/mcp-oauth/healthz` | Health check |
| `/.well-known/oauth-authorization-server` | Discovery Authorization Server (RFC 8414) |
| `/.well-known/oauth-protected-resource` | Discovery Resource Server (RFC 9728) |
| `POST /mcp-oauth/register` | Dynamic Client Registration (RFC 7591) |
| `GET /mcp-oauth/authorize` | Page de consentement Mehdi |
| `POST /mcp-oauth/authorize` | Reception du consentement -> redirect avec code |
| `POST /mcp-oauth/token` | Echange `authorization_code` + PKCE, ou `refresh_token` |
| `POST /mcp-oauth/revoke` | Revocation d'un token (RFC 7009) |
| `ANY /mcp-oauth/mcp` | Proxy vers `mcp-remote:8888/mcp` apres validation Bearer |

## Scopes

| Scope | Tools autorises |
|---|---|
| `read` | `search_*`, `get_entity`, `get_history`, `veille_juridique` |
| `write` | `read` + `save_memory`, `correct_fact`, `lexia_alert` |
| `admin` | Tout (pas de filtre) |

## Securite

- Tokens opaques stockes **hashes HMAC-SHA256** dans SQLite (jamais en clair).
- PKCE S256 obligatoire.
- Rate-limit : `/mcp` 60/min, `/token` 30/min, `/register` 10/min.
- Audit log dans `/data/audit.log` : enregistrement, consentement, emission token, appel MCP.
- Page de consentement protegee par `OAUTH_ADMIN_APPROVAL_TOKEN` (connu de Mehdi uniquement).
- Revocation d'urgence via `scripts/oauth-revoke-all.sh` (cote VPS).

## Integration MCP

Le proxy ne fait **aucune** logique metier : il relaie `/mcp-oauth/mcp` vers `http://mcp-remote:8888/mcp` en preservant le streaming. Il filtre cote reponse `tools/list` selon le scope, et bloque les `tools/call` interdits avec HTTP 403 `insufficient_scope`.

## Zero-impact sur l'existant

- Aucune modification de `mcp-remote-server.py`, `agea-bridge.py`, du bot ou de Caddy cote `/mcp`.
- Container separe (`docker-oauth-proxy-1`), volume separe (`oauth_data`), base SQLite isolee.
- Rollback : `docker compose stop oauth-proxy` + retrait des routes Caddy `/mcp-oauth*` et `/.well-known/*`.
