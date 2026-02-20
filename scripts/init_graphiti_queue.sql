-- ===========================================
-- AGEA Phase 5 — Queue Graphiti (PostgreSQL)
-- ===========================================
-- Table de queue pour le worker asynchrone Graphiti.
-- Utilise la base PostgreSQL existante (pas de Redis).
-- Idempotent : CREATE IF NOT EXISTS + UNIQUE constraint.
--
-- Usage :
--   docker compose exec -T postgres psql -U agea -d agea_memory -f /tmp/init_graphiti_queue.sql
-- ===========================================

CREATE TABLE IF NOT EXISTS graphiti_tasks (
    id SERIAL PRIMARY KEY,
    message_uuid VARCHAR(36) NOT NULL UNIQUE,
    content TEXT NOT NULL,
    source_description VARCHAR(255) DEFAULT 'telegram',
    task_type VARCHAR(20) DEFAULT 'add_episode',
    status VARCHAR(20) DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 5,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    error_message TEXT,
    next_retry_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_graphiti_tasks_status
    ON graphiti_tasks(status, next_retry_at);

CREATE INDEX IF NOT EXISTS idx_graphiti_tasks_created
    ON graphiti_tasks(created_at);
