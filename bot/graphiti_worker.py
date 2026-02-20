"""
GraphitiWorker - Worker asynchrone pour la queue Graphiti
=========================================================
Tourne en arriere-plan dans le meme process Bot (asyncio.create_task).
Consomme la table graphiti_tasks dans PostgreSQL.
Backoff exponentiel en cas d'echec, max 5 tentatives.
"""

import os
import uuid
import asyncio
import logging
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger("agea.graphiti.worker")

# Connection PostgreSQL directe via Zep's postgres
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://agea:password@postgres:5432/agea_memory",
)


class GraphitiWorker:
    """Worker asynchrone qui consomme la queue PostgreSQL."""

    def __init__(self, graphiti_client):
        """
        Args:
            graphiti_client: Instance de GraphitiClient
        """
        self.graphiti = graphiti_client
        self._running = False
        self._pool = None

    async def _get_pool(self):
        """Cree ou retourne le pool de connexion asyncpg."""
        if self._pool is None:
            try:
                import asyncpg
                self._pool = await asyncpg.create_pool(
                    POSTGRES_DSN,
                    min_size=1,
                    max_size=3,
                )
                logger.info("Pool PostgreSQL cree pour le worker")
            except Exception as e:
                logger.error("Erreur creation pool PostgreSQL: %s", e)
                raise
        return self._pool

    async def run(self):
        """Boucle principale du worker."""
        self._running = True
        logger.info("Worker Graphiti demarre")

        # Attendre que Graphiti soit pret
        while self._running and not self.graphiti.available:
            logger.info("Worker: attente initialisation Graphiti...")
            await asyncio.sleep(10)

        while self._running:
            try:
                tasks = await self._fetch_pending(batch_size=3)
                if not tasks:
                    await asyncio.sleep(10)
                    continue

                for task in tasks:
                    if not self._running:
                        break
                    await self._process_task(task)

                # Pause entre batches (respect rate limits)
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                logger.info("Worker Graphiti arrete (cancel)")
                break
            except Exception as e:
                logger.error("Erreur worker: %s", e)
                await asyncio.sleep(30)

        self._running = False
        if self._pool:
            await self._pool.close()
        logger.info("Worker Graphiti termine")

    async def stop(self):
        """Arrete le worker proprement."""
        self._running = False

    async def _fetch_pending(self, batch_size: int = 3) -> list[dict]:
        """Recupere les taches pending prets a traiter."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, message_uuid, content, source_description,
                       task_type, attempts
                FROM graphiti_tasks
                WHERE status = 'pending' AND next_retry_at <= $1
                ORDER BY created_at
                LIMIT $2
                """,
                datetime.utcnow(),
                batch_size,
            )
            return [dict(r) for r in rows]

    async def _process_task(self, task: dict):
        """Traite une tache individuelle."""
        task_id = task["id"]
        task_type = task["task_type"]
        content = task["content"]
        source = task["source_description"]

        logger.info("Traitement tache #%d (%s): %s...", task_id, task_type, content[:50])

        # Marquer processing
        await self._update_status(task_id, "processing")

        try:
            success = False
            if task_type == "add_episode":
                success = await self.graphiti.add_episode(content, source)
            elif task_type == "correct":
                success = await self.graphiti.correct_fact(content, source)
            elif task_type == "forget":
                negation = f"CORRECTION: L'information suivante est FAUSSE et obsolete: {content}"
                success = await self.graphiti.add_episode(negation, "Invalidation utilisateur")
            else:
                logger.warning("Type de tache inconnu: %s", task_type)
                await self._mark_failed(task_id, f"Type inconnu: {task_type}")
                return

            if success:
                await self._mark_done(task_id)
                logger.info("Tache #%d terminee avec succes", task_id)
            else:
                await self._mark_failed(task_id, "Graphiti returned False")

        except Exception as e:
            logger.error("Erreur tache #%d: %s", task_id, e)
            await self._mark_failed(task_id, str(e))

    async def _update_status(self, task_id: int, status: str):
        """Met a jour le statut d'une tache."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE graphiti_tasks SET status = $1 WHERE id = $2",
                status, task_id,
            )

    async def _mark_done(self, task_id: int):
        """Marque une tache comme terminee."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE graphiti_tasks
                SET status = 'done', processed_at = $1
                WHERE id = $2
                """,
                datetime.utcnow(), task_id,
            )

    async def _mark_failed(self, task_id: int, error: str):
        """
        Marque une tache comme echouee avec backoff exponentiel.
        Si max_attempts atteint → status = 'failed' (definitif).
        Sinon → status = 'pending' + next_retry_at = now + 2^attempts minutes.
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT attempts, max_attempts FROM graphiti_tasks WHERE id = $1",
                task_id,
            )
            if not row:
                return

            new_attempts = row["attempts"] + 1
            if new_attempts >= row["max_attempts"]:
                # Echec definitif
                await conn.execute(
                    """
                    UPDATE graphiti_tasks
                    SET status = 'failed', attempts = $1,
                        error_message = $2, processed_at = $3
                    WHERE id = $4
                    """,
                    new_attempts, error[:500], datetime.utcnow(), task_id,
                )
                logger.warning("Tache #%d echouee definitivement apres %d tentatives", task_id, new_attempts)
            else:
                # Retry avec backoff exponentiel
                delay_minutes = 2 ** new_attempts  # 2, 4, 8, 16, 32 min
                next_retry = datetime.utcnow() + timedelta(minutes=delay_minutes)
                await conn.execute(
                    """
                    UPDATE graphiti_tasks
                    SET status = 'pending', attempts = $1,
                        error_message = $2, next_retry_at = $3
                    WHERE id = $4
                    """,
                    new_attempts, error[:500], next_retry, task_id,
                )
                logger.info(
                    "Tache #%d: retry %d/%d dans %d min",
                    task_id, new_attempts, row["max_attempts"], delay_minutes,
                )

    # === Fonctions utilitaires (appelees par main.py) ===

    async def get_queue_stats(self) -> dict:
        """Retourne les stats de la queue."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT status, count(*) as count
                    FROM graphiti_tasks
                    GROUP BY status
                    """,
                )
                stats = {r["status"]: r["count"] for r in rows}
                return {
                    "pending": stats.get("pending", 0),
                    "processing": stats.get("processing", 0),
                    "done": stats.get("done", 0),
                    "failed": stats.get("failed", 0),
                    "total": sum(stats.values()),
                }
        except Exception as e:
            logger.error("Erreur get_queue_stats: %s", e)
            return {"error": str(e)}


async def enqueue_graphiti_task(
    content: str,
    task_type: str = "add_episode",
    source_description: str = "telegram",
    message_uuid: str = None,
) -> bool:
    """
    Ajoute une tache dans la queue PostgreSQL.
    Appele par main.py lors de /memo, /correct, /forget.
    Rapide : simple INSERT SQL.
    """
    if not message_uuid:
        message_uuid = str(uuid.uuid4())

    try:
        import asyncpg
        conn = await asyncpg.connect(POSTGRES_DSN)
        try:
            await conn.execute(
                """
                INSERT INTO graphiti_tasks (message_uuid, content, source_description, task_type)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (message_uuid) DO NOTHING
                """,
                message_uuid, content, source_description, task_type,
            )
            logger.info("Tache enqueue: %s (%s)", task_type, content[:50])
            return True
        finally:
            await conn.close()
    except Exception as e:
        logger.error("Erreur enqueue: %s", e)
        return False
