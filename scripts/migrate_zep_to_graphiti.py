"""
migrate_zep_to_graphiti.py - Migration idempotente Zep → Graphiti
=================================================================
Relit les messages Zep et les enqueue dans la queue PostgreSQL.
Le Worker Graphiti traite les taches en arriere-plan.

Idempotent grace a :
- message_uuid genere depuis le hash du contenu
- UNIQUE constraint + ON CONFLICT DO NOTHING dans graphiti_tasks

Usage :
    python scripts/migrate_zep_to_graphiti.py [--last-n 100] [--dry-run]
"""

import os
import sys
import json
import hashlib
import asyncio
import argparse
import logging
from datetime import datetime

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate")

# Configuration
AGEA_URL = os.getenv("AGEA_API_URL", "https://srv987452.hstgr.cloud")
AGEA_TOKEN = os.getenv("AGEA_API_TOKEN", os.getenv("ZEP_SECRET_KEY", ""))
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://agea:password@localhost:5432/agea_memory",
)
CHECKPOINT_FILE = os.path.join(os.path.dirname(__file__), "migrate_checkpoint.json")


def content_uuid(content: str) -> str:
    """Genere un UUID deterministe depuis le contenu (idempotence)."""
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def load_checkpoint() -> dict:
    """Charge le checkpoint de migration."""
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:
            return json.load(f)
    return {"migrated": [], "last_run": None, "total_enqueued": 0}


def save_checkpoint(checkpoint: dict):
    """Sauvegarde le checkpoint de migration."""
    checkpoint["last_run"] = datetime.utcnow().isoformat()
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoint, f, indent=2)


async def fetch_zep_messages(last_n: int = 100) -> list[dict]:
    """Recupere les messages depuis l'API AGEA (session Zep)."""
    headers = {
        "Authorization": f"Bearer {AGEA_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{AGEA_URL}/api/session/mehdi-agea/history",
            params={"last_n": last_n},
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error("Erreur API Zep: %d - %s", resp.status_code, resp.text)
            return []
        data = resp.json()
        return data.get("messages", [])


async def enqueue_to_postgres(messages: list[dict], dry_run: bool = False) -> int:
    """Enqueue les messages dans graphiti_tasks via asyncpg."""
    if dry_run:
        logger.info("[DRY RUN] %d messages seraient enqueues", len(messages))
        return len(messages)

    try:
        import asyncpg
    except ImportError:
        logger.error("asyncpg non installe. pip install asyncpg")
        return 0

    checkpoint = load_checkpoint()
    already_migrated = set(checkpoint.get("migrated", []))

    conn = await asyncpg.connect(POSTGRES_DSN)
    enqueued = 0

    try:
        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "unknown")

            if not content or len(content) < 10:
                continue

            msg_uuid = content_uuid(content)
            if msg_uuid in already_migrated:
                logger.debug("Deja migre: %s...", content[:40])
                continue

            source = f"migration zep ({role})"
            try:
                await conn.execute(
                    """
                    INSERT INTO graphiti_tasks (message_uuid, content, source_description, task_type)
                    VALUES ($1, $2, $3, 'add_episode')
                    ON CONFLICT (message_uuid) DO NOTHING
                    """,
                    msg_uuid, content, source,
                )
                already_migrated.add(msg_uuid)
                enqueued += 1
                logger.info("Enqueue: [%s] %s...", role, content[:50])
            except Exception as e:
                logger.error("Erreur enqueue: %s - %s", msg_uuid, e)

        # Sauvegarder checkpoint
        checkpoint["migrated"] = list(already_migrated)
        checkpoint["total_enqueued"] = checkpoint.get("total_enqueued", 0) + enqueued
        save_checkpoint(checkpoint)

    finally:
        await conn.close()

    return enqueued


async def main():
    parser = argparse.ArgumentParser(description="Migration Zep → Graphiti")
    parser.add_argument("--last-n", type=int, default=100, help="Nombre de messages a migrer")
    parser.add_argument("--dry-run", action="store_true", help="Simuler sans ecrire")
    args = parser.parse_args()

    logger.info("=== Migration Zep → Graphiti ===")
    logger.info("API: %s", AGEA_URL)
    logger.info("PostgreSQL: %s", POSTGRES_DSN.split("@")[1] if "@" in POSTGRES_DSN else "***")
    logger.info("Messages demandes: %d", args.last_n)
    logger.info("Mode: %s", "DRY RUN" if args.dry_run else "PRODUCTION")

    # 1. Recuperer les messages Zep
    messages = await fetch_zep_messages(last_n=args.last_n)
    logger.info("Messages recuperes: %d", len(messages))

    if not messages:
        logger.info("Aucun message a migrer.")
        return

    # 2. Enqueue dans PostgreSQL
    enqueued = await enqueue_to_postgres(messages, dry_run=args.dry_run)
    logger.info("Messages enqueues: %d / %d", enqueued, len(messages))

    # 3. Rapport
    checkpoint = load_checkpoint()
    logger.info("=== Rapport ===")
    logger.info("Total migre (cumule): %d", checkpoint.get("total_enqueued", 0))
    logger.info("Checkpoint: %s", CHECKPOINT_FILE)
    logger.info("Le Worker Graphiti traitera les taches en arriere-plan.")


if __name__ == "__main__":
    asyncio.run(main())
