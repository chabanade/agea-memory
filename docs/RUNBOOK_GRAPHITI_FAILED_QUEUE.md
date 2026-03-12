# RUNBOOK - Graphiti queue `failed`

Date de creation: 2026-03-12  
Contexte: incident recurrent AGEA avec relances Telegram `X tache(s) Graphiti en echec`.

## 1) Symptomes

- `/queue` montre `failed > 0` pendant plusieurs jours.
- Telegram envoie chaque matin: `Relances du matin ... X tache(s) Graphiti en echec`.
- Les nouvelles memoires affichent `Structuration en cours...` mais ne finissent pas toutes en `done`.

## 2) Cause racine constatee (incident du 2026-03-12)

- Erreur dominante dans les logs bot:
  - `429 RESOURCE_EXHAUSTED` sur `gemini-embedding-001`
  - quota depasse (`embed_content_free_tier_requests`)
- Effet de bord:
  - `Graphiti returned False` dans `graphiti_tasks.error_message`
  - retries puis passage en `failed` quand `max_attempts` est atteint.

## 3) Correctifs deja integres dans le code

Commits:

- `1638961`: resilence Graphiti (auto-reconnect + queue persistante)
- `51e063d`: outils admin pour diagnostiquer et relancer les `failed`

Fonctions ajoutees:

- `GET /api/admin/failed?limit=N`
- `POST /api/admin/requeue-failed?limit=N`
- commande Telegram `/queuefix [N]`

## 4) Procedure rapide (prod)

### 4.1 Verifier l etat

- Telegram: `/queue`
- API: `GET /api/graphiti/queue`

### 4.2 Diagnostiquer les erreurs reelles

- API: `GET /api/admin/failed?limit=10`

ou SQL:

```sql
SELECT id, task_type, attempts, max_attempts,
       LEFT(error_message, 180) AS err,
       LEFT(content, 120) AS content
FROM graphiti_tasks
WHERE status = 'failed'
ORDER BY processed_at DESC NULLS LAST
LIMIT 20;
```

### 4.3 Relancer les failed

- Telegram: `/queuefix 100`
- API: `POST /api/admin/requeue-failed?limit=200`

### 4.4 Si quota embeddings sature (429 Gemini)

Appliquer le garde-fou SQL temporaire:

```sql
UPDATE graphiti_tasks
SET max_attempts = 50
WHERE status IN ('pending','processing','failed')
  AND max_attempts < 50;

UPDATE graphiti_tasks
SET next_retry_at = GREATEST(next_retry_at, NOW() + INTERVAL '15 minutes')
WHERE status = 'pending' AND attempts > 0;
```

## 5) Verification post-correction

- `failed` doit revenir a `0`.
- `pending` peut rester > 0 tant que le quota externe est bloque.
- Surveiller 10-15 min:
  - `pending`/`processing` bougent
  - `failed` ne remonte pas.

## 6) Ameliorations recommandees

- Remplacer le free tier embeddings pour prod (provider payant ou local).
- Ajouter une gestion specifique du 429 quota dans le worker:
  - ne pas compter comme echec "dur"
  - deferer directement le retry (ex: +30 min / +2h).
- Ajouter une metrique dediee:
  - `failed_by_error_type` (quota, connection, parsing, autre).

## 7) Commandes utiles VPS

Exemple (depuis `/opt/agea/docker`):

```bash
docker compose --env-file ../.env exec -T postgres psql -U agea -d agea_memory -c "SELECT status, count(*) FROM graphiti_tasks GROUP BY status;"
docker compose --env-file ../.env logs --tail 120 bot
```
