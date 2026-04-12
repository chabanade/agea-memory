"""
VeilleJuridique - Veille legislative hebdomadaire automatique (Phase 3 LEXIA)
=============================================================================
Chaque lundi a 9h (Europe/Paris), interroge l'API Legifrance (JORF)
et envoie un resume Telegram si de nouveaux textes pertinents sont parus.

Domaines surveilles : ENR, BTP, marches publics, IRVE, fiscalite travaux.
"""

import os
import asyncio
import logging
from datetime import datetime, time, timezone, timedelta

logger = logging.getLogger("agea.veille-juridique")

PARIS_OFFSET = timedelta(hours=1)
PARIS_TZ = timezone(PARIS_OFFSET)

VEILLE_HOUR = int(os.getenv("VEILLE_JURIDIQUE_HOUR", "9"))
VEILLE_DAY = int(os.getenv("VEILLE_JURIDIQUE_DAY", "0"))  # 0=lundi
VEILLE_ENABLED = os.getenv("VEILLE_JURIDIQUE_ENABLED", "true").lower() == "true"


class VeilleJuridique:
    """Veille legislative hebdomadaire via Legifrance JORF."""

    def __init__(self, chat_id: str, send_fn, lexia_client):
        self.chat_id = chat_id
        self.send = send_fn
        self.lexia = lexia_client
        self._running = False

    async def run(self):
        if not VEILLE_ENABLED:
            logger.info("Veille juridique desactivee (VEILLE_JURIDIQUE_ENABLED=false)")
            return

        self._running = True
        logger.info(
            "Veille juridique activee (jour: %d, heure: %dh Paris)",
            VEILLE_DAY, VEILLE_HOUR,
        )

        while self._running:
            try:
                now = datetime.now(PARIS_TZ)

                # Trouver le prochain lundi (ou jour configure) a l'heure prevue
                days_ahead = VEILLE_DAY - now.weekday()
                if days_ahead < 0 or (days_ahead == 0 and now.hour >= VEILLE_HOUR):
                    days_ahead += 7

                target = datetime.combine(
                    now.date() + timedelta(days=days_ahead),
                    time(VEILLE_HOUR, 0),
                    PARIS_TZ,
                )

                wait_seconds = (target - now).total_seconds()
                logger.info("Prochaine veille dans %.1f jours", wait_seconds / 86400)
                await asyncio.sleep(wait_seconds)

                if not self._running:
                    break

                await self._run_veille()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Erreur veille juridique: %s", e)
                await asyncio.sleep(300)

    async def stop(self):
        self._running = False

    async def _run_veille(self):
        """Execute la veille et envoie le resume."""
        if not self.lexia or not self.lexia.enabled:
            logger.warning("Veille: LEXIA desactive, skip")
            return

        logger.info("Lancement veille juridique hebdomadaire")

        results = await self.lexia.check_veille(days=7)

        if not results:
            logger.info("Veille: aucun nouveau texte cette semaine")
            # Pas de message si rien de nouveau (eviter le bruit)
            return

        # Formater le message Telegram
        message = (
            "\U0001f4e2 <b>VEILLE LEXIA — Semaine du "
            f"{datetime.now(PARIS_TZ).strftime('%d/%m/%Y')}</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        )

        for i, r in enumerate(results[:8], 1):
            title = r.get("title", "Sans titre")[:120]
            keyword = r.get("keyword", "")
            date = r.get("date", "")[:10]
            message += f"{i}. <b>{keyword}</b> — {title}"
            if date:
                message += f" ({date})"
            message += "\n\n"

        message += (
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"{len(results)} texte(s) detecte(s) cette semaine.\n"
            "\U0001f4ac Tape /veille pour les details ou /loi pour rechercher."
        )

        await self.send(self.chat_id, message)
        logger.info("Veille envoyee: %d textes", len(results))
