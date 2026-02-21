"""
ProactiveAgent - Relances proactives Telegram (Phase 6E)
========================================================
Chaque matin a 8h (Europe/Paris), analyse le knowledge graph
et envoie des relances pertinentes :
- Projets sans mise a jour depuis X jours
- Rappels actifs
- Problemes ouverts non resolus
"""

import os
import asyncio
import logging
from datetime import datetime, time, timezone, timedelta

logger = logging.getLogger("agea.proactive")

PARIS_OFFSET = timedelta(hours=1)
PARIS_TZ = timezone(PARIS_OFFSET)

PROACTIVE_HOUR = int(os.getenv("PROACTIVE_HOUR", "8"))
PROACTIVE_ENABLED = os.getenv("PROACTIVE_ENABLED", "true").lower() == "true"
STALE_DAYS = int(os.getenv("PROACTIVE_STALE_DAYS", "5"))

POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://agea:password@postgres:5432/agea_memory",
)


class ProactiveAgent:
    """Envoie des relances proactives chaque matin."""

    def __init__(self, chat_id: str, send_fn, graphiti_client=None):
        self.chat_id = chat_id
        self.send = send_fn
        self.graphiti = graphiti_client
        self._running = False

    async def run(self):
        if not PROACTIVE_ENABLED:
            logger.info("Relances proactives desactivees (PROACTIVE_ENABLED=false)")
            return

        self._running = True
        logger.info("Relances proactives activees (heure: %dh Paris)", PROACTIVE_HOUR)

        while self._running:
            try:
                now = datetime.now(PARIS_TZ)
                target = datetime.combine(now.date(), time(PROACTIVE_HOUR, 0), PARIS_TZ)
                if now >= target:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info("Prochaines relances dans %.0fh", wait_seconds / 3600)
                await asyncio.sleep(wait_seconds)

                if not self._running:
                    break

                relances = await self._check_all()
                if relances:
                    message = "\U0001f514 Relances du matin :\n\n" + "\n\n".join(relances)
                    await self.send(self.chat_id, message)
                    logger.info("Relances envoyees: %d", len(relances))
                else:
                    logger.info("Aucune relance a envoyer")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Erreur relances proactives: %s", e)
                await asyncio.sleep(60)

    async def stop(self):
        self._running = False

    async def _check_all(self) -> list[str]:
        """Verifie toutes les sources de relances."""
        relances = []

        # 1. Rappels actifs (tagues [RAPPEL] dans la queue)
        rappels = await self._find_reminders()
        for r in rappels:
            relances.append(f"\u23f0 {r}")

        # 2. Problemes ouverts (tagues [PROBLEME] sans correction)
        problems = await self._find_open_problems()
        for p in problems:
            relances.append(f"\u26a0\ufe0f Probl\u00e8me ouvert ({p['days']}j) : {p['content']}")

        # 3. Projets silencieux (via Graphiti)
        stale = await self._find_stale_projects()
        for s in stale:
            relances.append(f"\U0001f4cd {s['name']} \u2014 pas de mise \u00e0 jour depuis {s['days']}j")

        # 4. Taches echouees dans la queue
        failed = await self._find_failed_tasks()
        if failed > 0:
            relances.append(f"\u274c {failed} t\u00e2che(s) Graphiti en \u00e9chec")

        return relances[:5]  # Max 5 pour ne pas spammer

    async def _find_reminders(self) -> list[str]:
        """Trouve les rappels recents (contenant [RAPPEL])."""
        try:
            import asyncpg
            conn = await asyncpg.connect(POSTGRES_DSN)
            try:
                cutoff = datetime.utcnow() - timedelta(days=7)
                rows = await conn.fetch(
                    """
                    SELECT content, created_at
                    FROM graphiti_tasks
                    WHERE content LIKE '%[RAPPEL]%'
                      AND task_type = 'add_episode'
                      AND status = 'done'
                      AND created_at >= $1
                    ORDER BY created_at DESC
                    LIMIT 5
                    """,
                    cutoff,
                )
                results = []
                for r in rows:
                    content = r["content"].replace("[RAPPEL] ", "").strip()
                    results.append(content[:120])
                return results
            finally:
                await conn.close()
        except Exception as e:
            logger.error("Erreur _find_reminders: %s", e)
            return []

    async def _find_open_problems(self) -> list[dict]:
        """Trouve les problemes non resolus."""
        try:
            import asyncpg
            conn = await asyncpg.connect(POSTGRES_DSN)
            try:
                cutoff = datetime.utcnow() - timedelta(days=14)
                rows = await conn.fetch(
                    """
                    SELECT content, created_at
                    FROM graphiti_tasks
                    WHERE content LIKE '%[PROBLEME]%'
                      AND task_type = 'add_episode'
                      AND status = 'done'
                      AND created_at >= $1
                    ORDER BY created_at DESC
                    LIMIT 3
                    """,
                    cutoff,
                )
                results = []
                now = datetime.utcnow()
                for r in rows:
                    content = r["content"].replace("[PROBLEME] ", "").strip()
                    days = (now - r["created_at"]).days
                    results.append({"content": content[:120], "days": days})
                return results
            finally:
                await conn.close()
        except Exception as e:
            logger.error("Erreur _find_open_problems: %s", e)
            return []

    async def _find_stale_projects(self) -> list[dict]:
        """Trouve les projets sans activite recente via Graphiti."""
        if not self.graphiti or not self.graphiti.read_enabled:
            return []

        try:
            # Recherche des entites de type chantier/projet
            facts = await self.graphiti.search("chantier projet", num_results=10)
            # Pour l'instant on retourne vide — necessiterait des queries Cypher
            # pour comparer les dates de derniere mise a jour par entite.
            # A implementer quand on aura plus de donnees dans le graphe.
            return []
        except Exception as e:
            logger.error("Erreur _find_stale_projects: %s", e)
            return []

    async def _find_failed_tasks(self) -> int:
        """Compte les taches echouees."""
        try:
            import asyncpg
            conn = await asyncpg.connect(POSTGRES_DSN)
            try:
                row = await conn.fetchrow(
                    "SELECT count(*) as count FROM graphiti_tasks WHERE status = 'failed'"
                )
                return row["count"] if row else 0
            finally:
                await conn.close()
        except Exception as e:
            logger.error("Erreur _find_failed_tasks: %s", e)
            return 0
