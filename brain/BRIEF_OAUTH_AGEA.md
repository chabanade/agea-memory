# Brief OAuth AGEA — Phase 18

**Crée** : 2026-04-21 (post reco diagnostic, session Claude Code + Cowork)
**Source** : lessons tirées du run 2026-04-21 (ORANGE→VERT) + `oauth-proxy/README.md` + lecture code `oauth_proxy.py`

## 1. Architecture

Container Docker isolé `docker-oauth-proxy-1` qui ajoute une couche OAuth 2.1 devant le serveur MCP AGEA, sans toucher au code `mcp-remote-server.py`.

- **Build** : `/opt/agea/oauth-proxy/` (Dockerfile + requirements.txt + oauth_proxy.py + templates/)
- **Port interne** : 8081
- **Volume persistant** : `oauth_data:/data` (SQLite + audit.log)
- **Dépend de** : `mcp-remote` (via `depends_on` dans docker-compose.yml)
- **Reverse proxy** : Caddy (`docker-caddy-1`) route `/mcp-oauth/*` et `/.well-known/oauth-*` vers oauth-proxy:8081

## 2. URL client MCP (à donner aux clients)

```
https://srv987452.hstgr.cloud/mcp-oauth/mcp
```

- À configurer dans **Claude Desktop** → Paramètres → Connecteurs → Ajouter un connecteur personnalisé
- **Pas** dans `claude_desktop_config.json` (le connecteur est stocké côté serveur Anthropic, pas en local)
- `claude.ai` web peut afficher le connecteur mais le flow OAuth se termine côté Claude Desktop

## 3. Endpoints exposés

| Path | Méthode | Rôle |
|---|---|---|
| `/healthz` | GET | Health check interne |
| `/mcp-oauth/healthz` | GET | Health check via Caddy |
| `/.well-known/oauth-authorization-server` | GET | Discovery AS (RFC 8414) — dual route root + `/mcp-oauth/` |
| `/.well-known/oauth-protected-resource` | GET | Discovery RS (RFC 9728) — dual route root + `/mcp-oauth/` |
| `/.well-known/openid-configuration` | GET | **Alias OIDC** ajouté 2026-04-21 (exigence Anthropic MCP hybrid) — dual route |
| `POST /mcp-oauth/register` | POST | Dynamic Client Registration (RFC 7591) |
| `GET /mcp-oauth/authorize` | GET | Page de consentement Mehdi (HTML) |
| `POST /mcp-oauth/authorize` | POST | Réception du consentement → redirect avec code |
| `POST /mcp-oauth/token` | POST | Échange authorization_code + PKCE, ou refresh_token |
| `POST /mcp-oauth/revoke` | POST | Révocation d'un token (RFC 7009) |
| `ANY /mcp-oauth/mcp` | ANY | Proxy vers `mcp-remote:8888/mcp` après validation Bearer + filtre scope |

## 4. Scopes

| Scope | Tools autorisés |
|---|---|
| `read` | `search_*`, `get_entity`, `get_history`, `veille_juridique` |
| `write` | `read` + `save_memory`, `correct_fact`, `lexia_alert` |
| `admin` | Tout (pas de filtre) |

## 5. Token admin (OAUTH_ADMIN_APPROVAL_TOKEN)

Mot de passe que Mehdi doit taper sur la page de consentement à chaque ajout de client MCP.

**Récupération depuis le VPS** :
```bash
ssh root@srv987452.hstgr.cloud "docker exec docker-oauth-proxy-1 env | grep OAUTH_ADMIN_APPROVAL_TOKEN"
```

**Régénération** : éditer `/opt/agea/docker/.env` champ `OAUTH_ADMIN_APPROVAL_TOKEN` + `docker compose up -d oauth-proxy`. Tous les consent futurs demanderont la nouvelle valeur.

## 6. Flow OAuth complet (pour comprendre les logs)

1. Client (Claude Desktop/Anthropic backend) → `GET /.well-known/oauth-protected-resource` → 200 + `authorization_servers: [ISSUER]`
2. Client → `GET /.well-known/oauth-authorization-server` → 200 + endpoints
3. Client → `GET /.well-known/openid-configuration` → 200 (même contenu que AS metadata, alias OIDC)
4. Client → `POST /mcp-oauth/register` → 201 avec `client_id=c_XXX` + `client_secret_hash` (DCR)
5. Client ouvre navigateur sur `GET /mcp-oauth/authorize?response_type=code&client_id=c_XXX&redirect_uri=https://claude.ai/api/mcp/auth_callback&code_challenge=...&state=...&scope=read+write+admin` → 200 page HTML consent
6. **Mehdi tape le token admin + clique "Autoriser"** → `POST /mcp-oauth/authorize` avec form field `approval_token=<valeur>` + `decision=allow`
7. Proxy → 302 redirect vers `https://claude.ai/api/mcp/auth_callback?code=<code>&state=<state>`
8. Anthropic backend → `POST /mcp-oauth/token` avec code + code_verifier PKCE → 200 + `access_token` + `refresh_token`
9. Client → `POST /mcp-oauth/mcp` avec `Authorization: Bearer <access_token>` → 200/202 (Streamable HTTP)

## 7. Erreurs courantes et diagnostic

### 403 `approval_token invalid` sur POST /authorize
Mehdi n'a pas tapé (ou mal tapé) le token admin sur la page consent. **Action** : retaper la valeur exacte de `OAUTH_ADMIN_APPROVAL_TOKEN` (récupérer via docker exec env).

### 404 sur `/.well-known/openid-configuration`
Avant 2026-04-21, l'endpoint n'existait pas. Anthropic MCP backend le requiert pourtant et abandonnait silencieusement le flow si absent (ne lançait plus POST /register). **Fix** : alias ajouté dans `oauth_proxy.py` qui renvoie le même contenu que `oauth-authorization-server`.

### "Couldn't reach the MCP server" côté claude.ai
Symptôme client. Cause racine : une étape du flow OAuth ci-dessus a échoué silencieusement côté serveur. **Diagnostic** : `docker logs docker-oauth-proxy-1 --since 5m | grep -v healthz` pour voir la dernière requête reçue et à quelle étape le flow s'est arrêté.

### Client "zombie" côté Claude Desktop
Connecteur affiché "Connecter" mais refuse de relancer le flow DCR. Cause : état mis en cache côté Anthropic (les tokens connecteurs sont stockés dans le compte utilisateur, pas en local Windows). **Fix** : supprimer le connecteur côté Claude Desktop ET côté claude.ai web, quitter l'app à fond, relancer, re-ajouter avec la même URL. Purger aussi les clients orphelins côté proxy :
```bash
docker exec docker-oauth-proxy-1 sqlite3 /data/oauth.db "DELETE FROM clients WHERE client_name='Claude';"
```

## 8. Sécurité

- **Tokens opaques** hashés HMAC-SHA256 dans SQLite, jamais en clair.
- **PKCE S256 obligatoire** (pas de downgrade possible).
- **Rate limits** : `/mcp` 60/min, `/token` 30/min, `/register` 10/min (slowapi via Redis).
- **Page consent** protégée par `OAUTH_ADMIN_APPROVAL_TOKEN` (barrière anti-auto-enrolment).
- **Audit log** `/data/audit.log` : `client_registered`, `consent_granted`, `consent_denied`, `token_issued`, `token_refreshed`, `token_revoked`, `mcp_call`.

## 9. Révocation d'urgence

Révoquer tous les tokens actifs (en cas de fuite suspectée) :
```bash
docker exec docker-oauth-proxy-1 sqlite3 /data/oauth.db "DELETE FROM tokens;"
```
Les clients devront refaire le flow OAuth complet au prochain appel.

Révoquer un client spécifique :
```bash
docker exec docker-oauth-proxy-1 sqlite3 /data/oauth.db "DELETE FROM clients WHERE client_id='c_XXX'; DELETE FROM tokens WHERE client_id='c_XXX';"
```

## 10. Rollback complet

Retirer la couche OAuth et repasser sur l'ancien MCP public :
```bash
cd /opt/agea/docker
docker compose stop oauth-proxy
# retirer aussi les directives Caddy /mcp-oauth/* et /.well-known/oauth-*
# redémarrer Caddy
```
Les clients devront se reconfigurer sur l'ancienne URL MCP (`/mcp` direct, sans OAuth).

## 11. Fichiers critiques

- `/opt/agea/oauth-proxy/oauth_proxy.py` — logique FastAPI (routes, DCR, consent, token, proxy MCP)
- `/opt/agea/oauth-proxy/templates/consent.html` — page HTML affichée à Mehdi
- `/opt/agea/oauth-proxy/Dockerfile` — image (+sqlite3 binary pour debug)
- `/opt/agea/docker/docker-compose.yml` — service oauth-proxy (build + env + volume)
- `/opt/agea/docker/.env` — `OAUTH_ADMIN_APPROVAL_TOKEN`, `OAUTH_JWT_SECRET`, TTLs
- `/var/backups/oauth_proxy.py.before-oidc-20260421T210519Z` — backup avant patch OIDC

## 12. Incident 2026-04-21 — résumé

Reconnexion Cowork à AGEA bloquée après chantier #2 include_invalidated. Diagnostic 3h :
1. Crainte initiale : chantier #2 a cassé OAuth → ÉLIMINÉ (container oauth-proxy Up 2 days, intact)
2. Hypothèse token expiré → ÉLIMINÉ (pas de TTL sur token admin)
3. Hypothèse rate-limit Anthropic → ÉLIMINÉ (même symptôme en navigation privée)
4. **Cause racine** : `/.well-known/openid-configuration` renvoyait 404. Anthropic MCP backend utilise ce endpoint comme pré-check AVANT de lancer DCR. 404 = abandon silencieux du flow, affichage "Couldn't reach" côté UI.
5. **Fix** : alias `openid-configuration` renvoyant la metadata AS. Container rebuilt, DB clients purgée, Mehdi a refait Connect → flow complet → token_issued → MCP actif.

**Leçon** : tout client OAuth 2.1 "moderne" tente aussi l'OIDC discovery. Toujours exposer `openid-configuration` même si on n'est pas strictly OIDC, pour la compat.
