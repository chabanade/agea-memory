-- ============================================
-- Table conversations - Remplace les sessions Zep
-- ============================================
-- Stocke l'historique conversationnel brut.
-- La recherche semantique est deleguee a Graphiti (Neo4j).
-- ============================================

CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL DEFAULT 'mehdi-agea',
    role VARCHAR(20) NOT NULL DEFAULT 'user',
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index pour get_memory() rapide (derniers N messages par session)
CREATE INDEX IF NOT EXISTS idx_conversations_session_created
    ON conversations(session_id, created_at DESC);
