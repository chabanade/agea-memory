"""
DailySummary - Resume quotidien Telegram (Phase 6D)
====================================================
Envoie un resume de la journee a 20h (Europe/Paris).
Cron interne via asyncio, lance dans le lifespan du bot.
"""

import os
import asyncio
import logging
from datetime import datetime, time, timezone, timedelta

logger = logging.getLogger("agea.summary")

# CET = UTC+1. Pour le DST on utilise +2 en ete, mais on simplifie ici.
# En France en fevrier on est en CET (UTC+1).
PARIS_OFFSET = timedelta(hours=1)
PARIS_TZ = timezone(PARIS_OFFSET)

SUMMARY_HOUR = int(os.getenv("DAILY_SUMMARY_HOUR", "20"))
SUMMARY_ENABLED = os.getenv("DAILY_SUMMARY_ENABLED", "true").lower() == "true"

POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://agea:password@postgres:5432/agea_memory",
)


class DailySummary:
    """Envoie un resume quotidien sur Telegram."""

    def __init__(self, chat_id: str, send_fn, graphiti_client=None):
        """
        Args:
            chat_id: Chat ID Telegram de Mehdi
            send_fn: Fonction async(chat_id, text) pour envoyer un message
            graphiti_client: Instance GraphitiClient (optionnel)
        """
        self.chat_id = chat_id
        self.send = send_fn
        self.graphiti = graphiti_client
        self._running = False

    async def run(self):
        """Boucle : attend l'heure cible chaque jour, envoie le resume."""
        if not SUMMARY_ENABLED:
            logger.info("Resume quotidien desactive (DAILY_SUMMARY_ENABLED=false)")
            return

        self._running = True
        logger.info("Resume quotidien active (heure: %dh Paris)", SUMMARY_HOUR)

        while self._running:
            try:
                now = datetime.now(PARIS_TZ)
                target = datetime.combine(now.date(), time(SUMMARY_HOUR, 0), PARIS_TZ)
                if now >= target:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info("Prochain resume dans %.0fh", wait_seconds / 3600)
                await asyncio.sleep(wait_seconds)

                if not self._running:
                    break

                summary = await self._build_summary()
                if summary:
                    await self.send(self.chat_id, summary)
                    logger.info("Resume quotidien envoye")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Erreur resume quotidien: %s", e)
                await asyncio.sleep(60)

    async def stop(self):
        self._running = False

    async def _build_summary(self) -> str | None:
        """Construit le resume de la journee."""
        today = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")
        stats = await self._get_today_stats()

        if stats["total"] == 0:
            return None  # Pas de memo = pas de resume

        lines = [f"\U0001f4cb R\u00e9sum\u00e9 du {today} :"]
        lines.append(f"\u2014 {stats['total']} m\u00e9mo(s) ajout\u00e9(s)")

        if stats.get("vocal", 0) > 0:
            lines.append(f"  dont {stats['vocal']} vocal(aux)")

        # Detail par tag metier
        tag_emojis = {
            "decision": "\U0001f4cc",
            "probleme": "\u26a0\ufe0f",
            "rappel": "\u23f0",
            "option": "\U0001f504",
            "constat": "\U0001f4cb",
        }
        for tag, emoji in tag_emojis.items():
            count = stats.get(tag, 0)
            if count > 0:
                lines.append(f"  {emoji} {count} {tag}(s)")

        if stats.get("corrections", 0) > 0:
            lines.append(f"\u2014 {stats['corrections']} correction(s)")

        if stats.get("forgets", 0) > 0:
            lines.append(f"\u2014 {stats['forgets']} oubli(s)")

        # Entites touchees via Graphiti
        entities = await self._get_today_entities()
        if entities:
            lines.append(f"\u2014 Entit\u00e9s touch\u00e9es : {', '.join(entities[:8])}")

        # Stats queue
        queue = await self._get_queue_stats()
        if queue.get("failed", 0) > 0:
            lines.append(f"\u2014 \u26a0\ufe0f {queue['failed']} t\u00e2che(s) Graphiti en \u00e9chec")
        if queue.get("pending", 0) > 0:
            lines.append(f"\u2014 {queue['pending']} t\u00e2che(s) en attente")

        return "\n".join(lines)

    async def _get_today_stats(self) -> dict:
        """Requete les taches du jour dans graphiti_tasks."""
        try:
            import asyncpg
            conn = await asyncpg.connect(POSTGRES_DSN)
            try:
                today_start = datetime.now(PARIS_TZ).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).astimezone(timezone.utc)

                rows = await conn.fetch(
                    """
                    SELECT task_type, source_description, content, status
                    FROM graphiti_tasks
                    WHERE created_at >= $1 AND status IN ('done', 'processing', 'pending')
                    """,
                    today_start,
                )

                stats = {"total": 0, "vocal": 0, "corrections": 0, "forgets": 0}
                for r in rows:
                    task_type = r["task_type"]
                    source = r["source_description"] or ""
                    content = r["content"] or ""

                    if task_type == "add_episode":
                        stats["total"] += 1
                    elif task_type == "correct":
                        stats["corrections"] += 1
                    elif task_type == "forget":
                        stats["forgets"] += 1

                    if "vocal" in source:
                        stats["vocal"] += 1

                    # Compter les tags metier
                    content_upper = content[:20].upper()
                    for tag in ["decision", "probleme", "rappel", "option", "constat"]:
                        if f"[{tag.upper()}]" in content_upper:
                            stats[tag] = stats.get(tag, 0) + 1

                return stats
            finally:
                await conn.close()
        except Exception as e:
            logger.error("Erreur _get_today_stats: %s", e)
            return {"total": 0}

    async def _get_today_entities(self) -> list[str]:
        """Recupere les entites touchees aujourd'hui via Graphiti."""
        if not self.graphiti or not self.graphiti.read_enabled:
            return []

        try:
            # Recherche generique pour trouver entites recentes
            facts = await self.graphiti.search("activite du jour", num_results=10)
            entities = set()
            for f in facts:
                name = f.get("name", "")
                if name and name not in ("HAS", "USES", "IS_OWNER_OF", "IS_FOUNDER_OF"):
                    entities.add(name)
            return list(entities)[:8]
        except Exception as e:
            logger.error("Erreur _get_today_entities: %s", e)
            return []

    async def _get_queue_stats(self) -> dict:
        """Stats de la queue Graphiti."""
        try:
            import asyncpg
            conn = await asyncpg.connect(POSTGRES_DSN)
            try:
                rows = await conn.fetch(
                    """
                    SELECT status, count(*) as count
                    FROM graphiti_tasks
                    GROUP BY status
                    """
                )
                stats = {r["status"]: r["count"] for r in rows}
                return {
                    "pending": stats.get("pending", 0),
                    "failed": stats.get("failed", 0),
                }
            finally:
                await conn.close()
        except Exception as e:
            logger.error("Erreur _get_queue_stats: %s", e)
            return {}
