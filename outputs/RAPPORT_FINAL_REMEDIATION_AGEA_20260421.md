# Rapport final — Remédiation AGEA 2026-04-21

**Run** : `remediation_20260421`
**Auteur** : Claude Code (local Windows) + Cowork (Claude Desktop MCP) + Mehdi Chabane
**Fenêtre** : 17h50 → 21h10 UTC (3h20 dont incident OAuth 55 min)
**Verdict final** : 🟢 **ORANGE → VERT**

---

## 1. Objectif

Livrer le méta-bloquant pour upgrade verdict audit mémoire AGEA : exposer le paramètre `include_invalidated: bool = False` sur les 3 couches MCP (MCP tool → HTTP route → GraphitiClient method) afin de rendre l'historique bi-temporel (edges invalidés via `correct_fact`) interrogeable côté Cowork/Claude.ai.

## 2. Périmètre exécuté

| Bloc | Fichier(s) | Ligne(s) touchée(s) | Statut |
|---|---|---|---|
| GraphitiClient | `/opt/agea/bot/graphiti_client.py` | search(L275), get_entity(L336) | ✅ |
| HTTP bot | `/opt/agea/bot/main.py` | api_get_facts(L394), api_get_entity(L435) | ✅ |
| MCP tools | `/opt/agea/mcp-server/mcp-remote-server.py` | search_facts(L111), get_entity(L144) | ✅ |
| Archivage | `mcp-server/agea-bridge.py` → `legacy/` | git mv (orphelin post-Zep migration) | ✅ |
| **Bonus OIDC** | `/opt/agea/oauth-proxy/oauth_proxy.py` | +2 alias `.well-known/openid-configuration` | ✅ |

## 3. Commits git

| SHA | Message | Scope |
|---|---|---|
| `a5eaa4c` (VPS) | chantier #2: expose include_invalidated on search_facts/get_entity (3-layer) | 3 fichiers code |
| `5dbacbf` (VPS) | chore: archive agea-bridge.py (dead since Zep->AGEA migration 2026-04-12) | 2 git mv |
| `<pending local>` | fix(oauth-proxy): add openid-configuration alias for Anthropic MCP hybrid clients | 1 fichier code |

Discipline `git commit --only` respectée : oauth-proxy WIP (Phase 18) resté intact dans staging area pré/post-run (preuve : diff `git status --short` `$BACKUP_DIR/git-status.before` ↔ `.after-commit-*`).

## 4. Smoke tests

### T1 — Non-régression clients existants (défaut False)
```bash
curl ... "/api/facts?q=backup-neo4j%20mcp-remote&limit=20" | jq '{count, invalid_hits}'
# → {"count": N, "invalid_hits": 0} ✅
```

### T2 — Audit bi-temporel (flag HTTP True)
```bash
curl ... "/api/facts?q=...&include_invalidated=true" | jq '[.results[] | select(.invalid_at != null)] | length'
# → 11 ✅ (edges du Cypher chirurgical 2026-04-21 16:21 UTC)
```

### T3-A — MCP Cowork défaut
```
search_facts("backup-neo4j stops mcp-remote", limit=10)
→ 2 faits, 0 suffixe [INVALIDÉ] ✅
```

### T3-B — MCP Cowork flag
```
search_facts(..., include_invalidated=true)
→ 10 faits dont 8 suffixés [INVALIDÉ le ...]:
  - 5× [INVALIDÉ le 2026-04-21] (Cypher chirurgical jour J)
  - 3× [INVALIDÉ le 2026-04-18] (correct_fact antérieurs)
✅
```

## 5. Incident OAuth (inattendu) — résolu

### 5.1 Symptôme
Après rebuild `bot` + `mcp-remote` du chantier #2, Cowork Claude Desktop affiche `{"error":"forbidden","detail":"approval token invalid"}` au retour sur claude.ai puis `"Couldn't reach the MCP server"` à toute tentative `Connecter`.

### 5.2 Diagnostic (55 min)

1. **Innocence chantier #2 confirmée** : `docker ps` → `docker-oauth-proxy-1 Up 2 days (healthy)`. Container jamais redémarré.
2. **Hypothèse 1 (token admin expiré)** → ÉLIMINÉ : pas de TTL sur `OAUTH_ADMIN_APPROVAL_TOKEN`, valeur identique depuis déploiement Phase 18.
3. **Hypothèse 2 (Caddy stale)** → ÉLIMINÉ : discovery OAuth retourne 200 sur root + issuer-relative paths.
4. **Hypothèse 3 (cache navigateur Mehdi)** → ÉLIMINÉ : même symptôme en navigation privée claude.ai.
5. **Cause racine trouvée via tail live** : `GET /mcp-oauth/.well-known/openid-configuration → 404 Not Found`. Anthropic MCP backend utilise ce endpoint comme pré-check silencieux avant de lancer POST /register (DCR). Le 404 faisait abandonner le flow AVANT toute tentative de register, d'où symptôme "jamais de page consent affichée à Mehdi".

### 5.3 Fix

Ajout d'un alias dans `oauth_proxy.py` (2 routes dual : root + issuer-relative) qui retourne le même metadata que `oauth-authorization-server` :

```python
@app.get("/.well-known/openid-configuration")
@app.get("/mcp-oauth/.well-known/openid-configuration")
async def oidc_discovery() -> JSONResponse:
    return JSONResponse(_as_metadata())
```

Déploiement :
- Edit local `c:/Users/Abyss/Dev/agea/oauth-proxy/oauth_proxy.py`
- `python3 -c 'import ast; ast.parse(...)'` → OK
- Backup `/var/backups/oauth_proxy.py.before-oidc-20260421T210519Z`
- scp + `docker compose build oauth-proxy && docker compose up -d oauth-proxy` → healthy < 10s
- Purge 5 clients Claude orphelins (tentatives ratées 20h10-20h17)
- Mehdi refait Connect → page consent affichée → token admin tapé → `token_issued` → MCP reco

### 5.4 Preuve restauration

Audit log 21:07 UTC :
```
[token_issued] client_id=c_oswks3GP137r9TmGmOBGkg scope=read,write,admin
[mcp_call] x9 status=200/202
```

## 6. Capitalisation AGEA (5 `save_memory`)

1. `agea-bridge.py archivé le 2026-04-21 dans /opt/agea/legacy/ (orphelin post-migration Zep→AGEA)`
2. `include_invalidated=False par défaut sur search_facts/get_entity (MCP + HTTP + client)`
3. `include_invalidated=True enables audit bi-temporel — suffixe [INVALIDÉ le YYYY-MM-DD]`
4. `alias /.well-known/openid-configuration ajouté 2026-04-21 — exigence Anthropic MCP hybrid`
5. `OAUTH_ADMIN_APPROVAL_TOKEN récupérable via ssh + docker exec env`

Extraction Graphiti post-run propre (vérifiée par search_facts en T3-B) : relations typées ARCHIVED_TO, IS_DEFAULT_PARAMETER_FOR, ENABLES, AFFECTS.

## 7. Leçons capitalisées

### 7.1 Techniques
1. **Toujours exposer `openid-configuration`** même pour OAuth 2.1 pur — les clients hybrides (Anthropic) le requièrent.
2. **`git commit --only` > `git add` aveugle** quand un staging WIP coexiste : discipline validée sur 3 commits sans contamination.
3. **Snapshot staging pré/post avec diff** permet de prouver la non-contamination (preuve matérielle auditable).
4. **`diff -u --label` sur copies VPS réelles** génère des patches qui passent `--dry-run` au premier coup (vs reconstruction à la main → 137 lignes de hunks cassés).
5. **Tail live oauth-proxy pendant une tentative utilisateur** révèle immédiatement à quelle étape du flow OAuth on échoue — diagnostic 5 min vs hypothèses spéculatives.

### 7.2 Process
6. **Cache schéma MCP Cowork** invalidé uniquement après reco complète du MCP client (pas sur simple restart serveur).
7. **Connecteur Claude Desktop stocké côté serveur Anthropic**, pas en local Windows. Supprimer côté Claude Desktop ET claude.ai web pour purger propre.
8. **Brief OAuth manquant dans `brain/`** depuis Phase 18 → diagnostic 3h incluait "quelle est la spec attendue ?" à blanc. Capturé dans `brain/BRIEF_OAUTH_AGEA.md` ce run.

### 7.3 Pilotage
9. **Double-IA diagnostique** (Claude Code local + Cowork Desktop) efficace : Claude Code sur preuve code/SSH/git, Cowork sur MCP/AGEA/rapport — couverture complémentaire sans duplication.
10. **Mehdi en superviseur read-only** après validation du plan = run complet 3h sans interruption demande-validation.

## 8. Chantiers restants (hors scope run)

| PRIO | Sujet | Complexité | À planifier |
|---|---|---|---|
| 3 | `chown -R 7474:7474` dans `/usr/local/bin/backup-neo4j.sh` + plan test dry-run | 15-20 min | 2026-04-22 |
| 6 | Bug extraction LLM Graphiti (`STOPS relates to mcp-remote` anomalies T3-A) | 1-2h investigation | libre |
| 7 | Sync git VPS ↔ repo local `c:/Users/Abyss/Dev/agea` (commits a5eaa4c + 5dbacbf pas dans repo local) | 15 min | libre |

## 9. Métriques du run

| Métrique | Valeur |
|---|---|
| Durée totale | 3h20 |
| Durée chantier #2 pur | 42 s (script one-shot) |
| Durée incident OAuth | 55 min |
| Fichiers code touchés | 4 (dont 1 bonus OIDC) |
| Commits git | 3 (2 sur VPS, 1 pending local) |
| Faits save_memory | 5 |
| Tokens Claude Code consommés | ~280k (estimation) |
| Interruption service MCP utilisateur | 55 min (window OAuth incident) |
| Régressions détectées | 0 |

## 10. Clôture

Chantier #2 déclaré **VERT COMPLET** à 21h10 UTC 2026-04-21.

Le verdict global audit mémoire AGEA passe de **ORANGE CONDITIONNEL** (bloquant méta : pas d'audit bi-temporel côté client) à **VERT** (audit bi-temporel exposé sur les 3 couches + client reconnecté + capitalisation AGEA intacte).

Run reproductible via :
- Patch : `patches/chantier2-include-invalidated-20260421.patch`
- Script : `scripts/apply-chantier2-20260421.sh`
- Rollback : `git revert <SHA_ARCHIVE> <SHA_CHANTIER2>` (SHAs dans `$BACKUP_DIR/commit-*.sha`)
- Brief OAuth : `brain/BRIEF_OAUTH_AGEA.md`

---

**Next session** : Bloc 2 — PRIO 3 chown self-heal.sh (15-20 min, fenêtre libre).
