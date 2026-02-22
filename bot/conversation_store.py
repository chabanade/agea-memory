"""
ConversationStore - Stockage PostgreSQL pour l'historique conversationnel
=========================================================================
Remplace ZepClient apres la migration Zep -> Graphiti Standalone.
Stocke les messages dans une table `conversations` (PostgreSQL).
La recherche semantique est deleguee a Graphiti (Neo4j).
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("agea.conversations")

POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://agea:password@postgres:5432/agea_memory",
)


class ConversationStore:
    """Remplace ZepClient — stockage historique dans PostgreSQL."""

    def __init__(self):
        self._pool = None

    async def initialize(self):
        """Cree le pool asyncpg et la table si necessaire."""
        import asyncpg

        self._pool = await asyncpg.create_pool(
            POSTGRES_DSN, min_size=1, max_size=5
        )

        # Auto-creation de la table (idempotent)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id SERIAL PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL DEFAULT 'mehdi-agea',
                    role VARCHAR(20) NOT NULL DEFAULT 'user',
                    content TEXT NOT NULL,
                    metadata JSONB DEFAULT '{}',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            # Index pour get_memory rapide
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_session_created
                ON conversations(session_id, created_at DESC);
            """)

        logger.info("ConversationStore initialise (PostgreSQL)")

    async def close(self):
        """Ferme le pool asyncpg."""
        if self._pool:
            await self._pool.close()
            logger.info("ConversationStore ferme")

    async def add_memory(
        self,
        text: str,
        role: str = "user",
        session_id: str = "mehdi-agea",
        metadata: Optional[dict] = None,
    ) -> bool:
        """Ajoute un message a l'historique. Retourne True si OK."""
        if not self._pool:
            logger.error("Pool non initialise")
            return False

        try:
            import json as json_mod

            meta_json = json_mod.dumps(metadata) if metadata else "{}"
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO conversations (session_id, role, content, metadata)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    session_id,
                    role,
                    text,
                    meta_json,
                )
            logger.info("Conversation ajoutee: %s...", text[:50])
            return True
        except Exception as e:
            logger.error("Erreur add_memory: %s", e)
            return False

    async def get_memory(
        self,
        session_id: str = "mehdi-agea",
        last_n: int = 10,
    ) -> Optional[dict]:
        """
        Recupere les N derniers messages d'une session.
        Format de retour compatible avec l'ancien ZepClient :
        {"messages": [{"role": "...", "content": "...", "created_at": "..."}]}
        """
        if not self._pool:
            return None

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT role, content, created_at
                    FROM conversations
                    WHERE session_id = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    session_id,
                    last_n,
                )

            # Inverser pour ordre chronologique (plus ancien en premier)
            messages = [
                {
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in reversed(rows)
            ]

            return {"messages": messages}
        except Exception as e:
            logger.error("Erreur get_memory: %s", e)
            return None
