"""
ZepClient - Client pour Zep Memory Engine v0.27
================================================
Gere les sessions, la memoire et la recherche dans Zep.
API REST : http://zep:8080/api/v1/
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger("agea.zep")

ZEP_API_URL = os.getenv("ZEP_API_URL", "http://localhost:8080")
ZEP_SECRET_KEY = os.getenv("ZEP_SECRET_KEY", "")

# Session par defaut pour Mehdi (single-user)
DEFAULT_SESSION_ID = "mehdi-agea"


class ZepClient:
    """Client pour l'API Zep v0.27."""

    def __init__(self):
        self.base_url = f"{ZEP_API_URL}/api/v1"
        self.headers = {"Content-Type": "application/json"}
        if ZEP_SECRET_KEY:
            self.headers["Authorization"] = f"Bearer {ZEP_SECRET_KEY}"

    async def ensure_session(self, session_id: str = DEFAULT_SESSION_ID) -> bool:
        """Cree la session si elle n'existe pas. Retourne True si OK."""
        async with httpx.AsyncClient(timeout=30) as client:
            # Verifier si la session existe
            resp = await client.get(
                f"{self.base_url}/sessions/{session_id}",
                headers=self.headers,
            )
            if resp.status_code == 200:
                return True

            # Creer la session
            resp = await client.post(
                f"{self.base_url}/sessions",
                headers=self.headers,
                json={
                    "session_id": session_id,
                    "metadata": {"user": "mehdi", "source": "telegram"},
                },
            )
            if resp.status_code in (200, 201):
                logger.info("Session Zep creee: %s", session_id)
                return True

            logger.error("Erreur creation session: %s %s", resp.status_code, resp.text)
            return False

    async def add_memory(
        self,
        text: str,
        role: str = "user",
        session_id: str = DEFAULT_SESSION_ID,
    ) -> bool:
        """Ajoute un message a la memoire Zep. Retourne True si OK."""
        await self.ensure_session(session_id)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/sessions/{session_id}/memory",
                headers=self.headers,
                json={
                    "messages": [
                        {
                            "role": role,
                            "content": text,
                        }
                    ],
                },
            )

            if resp.status_code in (200, 201):
                logger.info("Memoire ajoutee: %s...", text[:50])
                return True

            logger.error("Erreur ajout memoire: %s %s", resp.status_code, resp.text)
            return False

    async def search(
        self,
        query: str,
        limit: int = 5,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> list[dict]:
        """
        Recherche semantique dans la memoire Zep.
        Retourne une liste de resultats avec score et contenu.
        """
        await self.ensure_session(session_id)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/sessions/{session_id}/search",
                headers=self.headers,
                json={
                    "text": query,
                    "search_type": "similarity",
                    "limit": limit,
                },
            )

            if resp.status_code != 200:
                logger.error("Erreur recherche: %s %s", resp.status_code, resp.text)
                return []

            data = resp.json()
            results = []
            for item in data:
                msg = item.get("message", {})
                results.append({
                    "content": msg.get("content", ""),
                    "role": msg.get("role", ""),
                    "score": item.get("dist", 0),
                    "created_at": msg.get("created_at", ""),
                })

            logger.info("Recherche '%s': %d resultats", query[:30], len(results))
            return results

    async def get_memory(
        self,
        session_id: str = DEFAULT_SESSION_ID,
        last_n: int = 10,
    ) -> Optional[dict]:
        """Recupere la memoire recente d'une session."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base_url}/sessions/{session_id}/memory",
                headers=self.headers,
                params={"lastn": last_n},
            )

            if resp.status_code != 200:
                logger.error("Erreur get memory: %s %s", resp.status_code, resp.text)
                return None

            return resp.json()
