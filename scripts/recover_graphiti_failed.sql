-- ==========================================
-- AGEA - Recovery script for Graphiti failed
-- ==========================================
-- Usage (from /opt/agea/docker):
-- docker compose --env-file ../.env exec -T postgres \
--   psql -U agea -d agea_memory < /opt/agea/scripts/recover_graphiti_failed.sql
--
-- Objectif:
-- 1) remettre les taches failed en pending
-- 2) eviter un retour immediat en failed si quota embeddings sature
-- ==========================================

-- 0) Etat avant
SELECT status, count(*) AS count
FROM graphiti_tasks
GROUP BY status
ORDER BY status;

-- 1) Diagnostic rapide des failed
SELECT id, task_type, attempts, max_attempts,
       LEFT(error_message, 180) AS error_preview,
       LEFT(content, 120) AS content_preview
FROM graphiti_tasks
WHERE status = 'failed'
ORDER BY processed_at DESC NULLS LAST
LIMIT 20;

-- 2) Garde-fou retry (temporaire)
ALTER TABLE graphiti_tasks
ALTER COLUMN max_attempts SET DEFAULT 50;

UPDATE graphiti_tasks
SET max_attempts = 50
WHERE status IN ('pending','processing','failed')
  AND max_attempts < 50;

UPDATE graphiti_tasks
SET next_retry_at = GREATEST(next_retry_at, NOW() + INTERVAL '15 minutes')
WHERE status = 'pending' AND attempts > 0;

-- 3) Requeue des failed
UPDATE graphiti_tasks
SET status = 'pending',
    attempts = 0,
    error_message = NULL,
    next_retry_at = CURRENT_TIMESTAMP,
    processed_at = NULL
WHERE status = 'failed';

-- 4) Etat apres
SELECT status, count(*) AS count
FROM graphiti_tasks
GROUP BY status
ORDER BY status;
