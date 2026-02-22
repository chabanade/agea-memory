"""
MCP Remote Server — AGEA Memory Bridge (Streamable HTTP)
========================================================
Expose les outils memoire AGEA en Streamable HTTP pour Claude.ai.
Proxy vers l'API REST existante (bot:8000) — zero logique metier.

Transport : Streamable HTTP (stateless, JSON responses)
Auth : Token interne vers l'API REST (AGEA_API_TOKEN)
Endpoint : /mcp (defaut SDK)
"""

import os
import json
import logging

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("agea.mcp-remote")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

AGEA_API_URL = os.getenv("AGEA_API_URL", "http://bot:8000")
AGEA_API_TOKEN = os.getenv("AGEA_API_TOKEN", "")

mcp = FastMCP(
    "AGEA Memory",
    host="0.0.0.0",
    port=8888,
    stateless_http=True,
    json_response=True,
    instructions=(
        "Memoire persistante de Mehdi (HEXAGONE ENERGIE). "
        "Utilise search_memory/search_facts pour chercher du contexte, "
        "get_entity pour les details d'une entite, "
        "save_memory pour sauvegarder, correct_fact pour corriger un fait, "
        "get_history pour l'historique recent, "
        "search_decisions pour les decisions de raisonnement."
    ),
)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {AGEA_API_TOKEN}",
        "Content-Type": "application/json",
    }


@mcp.tool()
async def search_memory(query: str, limit: int = 5) -> str:
    """Cherche dans la memoire AGEA par recherche semantique.

    Args:
        query: La question ou le sujet a rechercher
        limit: Nombre max de resultats (defaut: 5)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{AGEA_API_URL}/api/context",
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
    """Sauvegarde une information dans la memoire AGEA.
    Utilise cette fonction quand Mehdi prend une decision importante,
    mentionne un fait nouveau, ou quand il faut retenir quelque chose.

    Args:
        content: L'information a sauvegarder
        role: Le role (user ou assistant, defaut: assistant)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{AGEA_API_URL}/api/memo",
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
async def search_facts(query: str, limit: int = 5) -> str:
    """Cherche des faits structures dans le knowledge graph AGEA/Graphiti.
    Plus precis que search_memory pour les donnees verifiees (entites, relations).

    Args:
        query: La question ou le sujet a rechercher
        limit: Nombre max de resultats (defaut: 5)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{AGEA_API_URL}/api/facts",
            params={"q": query, "limit": limit},
            headers=_headers(),
        )
        if resp.status_code != 200:
            return f"Erreur API: {resp.status_code} - {resp.text}"
        data = resp.json()
        source = data.get("source", "unknown")
        results = data.get("results", [])
        if not results:
            return f"Aucun fait trouve pour '{query}'"
        lines = []
        for r in results:
            fact = r.get("fact", "")
            name = r.get("name", "")
            prefix = f"[{name}] " if name else ""
            lines.append(f"{prefix}{fact}")
        return f"[{source}] {len(lines)} faits pour '{query}':\n" + "\n".join(lines)


@mcp.tool()
async def get_entity(name: str) -> str:
    """Recupere les details et relations d'une entite du knowledge graph.

    Args:
        name: Nom de l'entite (ex: "Mehdi", "HEXAGONE ENERGIE", "CNVL")
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{AGEA_API_URL}/api/entity/{name}",
            headers=_headers(),
        )
        if resp.status_code == 200:
            return json.dumps(resp.json(), ensure_ascii=False, indent=2)
        elif resp.status_code == 404:
            return f"Entite '{name}' non trouvee dans le knowledge graph."
        else:
            return f"Erreur {resp.status_code}: {resp.text}"


@mcp.tool()
async def correct_fact(correction: str) -> str:
    """Corrige un fait dans la memoire avec bi-temporalite.
    L'ancien fait est invalide et le nouveau est enregistre.

    Args:
        correction: Description de la correction
            (ex: "Pour le CNVL c'est de la tuile canal pas romaine")
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{AGEA_API_URL}/api/correct",
            json={"content": correction},
            headers=_headers(),
        )
        if resp.status_code == 200:
            return f"Correction enregistree : {correction}"
        else:
            return f"Erreur {resp.status_code}: {resp.text}"


@mcp.tool()
async def get_history(last_n: int = 10) -> str:
    """Recupere les N derniers messages de la memoire AGEA.

    Args:
        last_n: Nombre de messages a recuperer (defaut: 10)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{AGEA_API_URL}/api/session/mehdi-agea/history",
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


@mcp.tool()
async def search_decisions(query: str, limit: int = 5) -> str:
    """Cherche des decisions structurees dans la memoire AGEA.
    Retourne les decisions, doutes et apprentissages lies a la requete.

    Args:
        query: Le sujet de la decision a chercher (ex: "Huawei CNVL")
        limit: Nombre max de resultats (defaut: 5)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{AGEA_API_URL}/api/facts",
            params={"q": f"D\u00c9CISION {query}", "limit": limit},
            headers=_headers(),
        )
        if resp.status_code != 200:
            return f"Erreur API: {resp.status_code} - {resp.text}"
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return f"Aucune decision trouvee pour '{query}'"
        lines = []
        for r in results:
            fact = r.get("fact", "")
            name = r.get("name", "")
            prefix = f"[{name}] " if name else ""
            lines.append(f"{prefix}{fact}")
        return f"{len(lines)} decision(s) pour '{query}':\n" + "\n".join(lines)


if __name__ == "__main__":
    logger.info("Demarrage MCP Remote Server AGEA sur port 8888")
    mcp.run(transport="streamable-http")
