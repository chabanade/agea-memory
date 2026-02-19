"""
MCP Server Bridge — Memoire AGEA/Zep via API REST
===================================================
Expose 3 outils MCP (search_memory, save_memory, get_history)
qui appellent les endpoints REST du bot AGEA (Phase 1).

Transport : STDIO (pour Claude Code / Cursor)
Config : variables AGEA_API_URL et AGEA_API_TOKEN
"""

import os
import json

import httpx
from mcp.server.fastmcp import FastMCP

AGEA_URL = os.getenv("AGEA_API_URL", "https://srv987452.hstgr.cloud")
AGEA_TOKEN = os.getenv("AGEA_API_TOKEN", "")

mcp = FastMCP(
    "zep-memory",
    instructions="Memoire persistante AGEA/HEXAGONE ENERGIE. "
    "Utilise search_memory pour chercher du contexte, "
    "save_memory pour sauvegarder des decisions/informations, "
    "get_history pour voir l'historique recent.",
)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {AGEA_TOKEN}",
        "Content-Type": "application/json",
    }


@mcp.tool()
async def search_memory(query: str, limit: int = 5) -> str:
    """Cherche dans la memoire AGEA/Zep par recherche semantique.

    Args:
        query: La question ou le sujet a rechercher
        limit: Nombre max de resultats (defaut: 5)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{AGEA_URL}/api/context",
            params={"q": query, "limit": limit},
            headers=_headers(),
        )
        if resp.status_code != 200:
            return f"Erreur API: {resp.status_code} - {resp.text}"
        data = resp.json()
        if not data.get("results"):
            return f"Aucun resultat pour '{query}'"
        lines = []
        for r in data["results"]:
            score = r.get("score", 0)
            content = r.get("content", "")
            role = r.get("role", "")
            lines.append(f"[{role}, score={score:.2f}] {content}")
        return f"{len(lines)} resultats pour '{query}':\n" + "\n".join(lines)


@mcp.tool()
async def save_memory(content: str, role: str = "assistant") -> str:
    """Sauvegarde une information dans la memoire AGEA/Zep.

    Args:
        content: L'information a sauvegarder
        role: Le role (user ou assistant, defaut: assistant)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{AGEA_URL}/api/memo",
            json={"content": content, "role": role},
            headers=_headers(),
        )
        if resp.status_code != 200:
            return f"Erreur API: {resp.status_code} - {resp.text}"
        data = resp.json()
        if data.get("ok"):
            return f"Sauvegarde OK: {content[:80]}"
        return f"Echec sauvegarde: {json.dumps(data)}"


@mcp.tool()
async def get_history(last_n: int = 10) -> str:
    """Recupere les N derniers messages de la memoire AGEA.

    Args:
        last_n: Nombre de messages a recuperer (defaut: 10)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{AGEA_URL}/api/session/mehdi-agea/history",
            params={"last_n": last_n},
            headers=_headers(),
        )
        if resp.status_code != 200:
            return f"Erreur API: {resp.status_code} - {resp.text}"
        data = resp.json()
        messages = data.get("messages", [])
        if not messages:
            return "Aucun message dans l'historique"
        lines = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")[:150]
            lines.append(f"[{role}] {content}")
        return f"{len(lines)} messages recents:\n" + "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
