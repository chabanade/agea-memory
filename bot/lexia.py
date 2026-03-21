"""
LEXIA - Agent Juridique HEXAGON ENR
====================================
Client API Legifrance (PISTE/DILA) + Judilibre.
Recherche dans les codes, lois, decrets et jurisprudences.
Stockage PostgreSQL (legal_articles + legal_alerts).
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import httpx
import asyncpg

logger = logging.getLogger("lexia")

# --- Configuration ---
PISTE_CLIENT_ID = os.getenv("PISTE_CLIENT_ID", "")
PISTE_CLIENT_SECRET = os.getenv("PISTE_CLIENT_SECRET", "")
PISTE_TOKEN_URL = os.getenv(
    "PISTE_TOKEN_URL", "https://oauth.piste.gouv.fr/api/oauth/token"
)
LEGIFRANCE_API_URL = os.getenv(
    "LEGIFRANCE_API_URL",
    "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app",
)
JUDILIBRE_API_URL = os.getenv(
    "JUDILIBRE_API_URL",
    "https://api.piste.gouv.fr/cassation/judilibre/v1.0",
)
LEXIA_ENABLED = os.getenv("LEXIA_ENABLED", "false").lower() == "true"
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN", "postgresql://agea:password@postgres:5432/agea_memory"
)

# Mots-cles ENR pour la veille
VEILLE_KEYWORDS = [
    "photovoltaique", "photovoltaïque", "energie renouvelable",
    "tarif achat", "obligation achat", "autoconsommation",
    "RGE", "NF C 15-100", "DTU", "CRE",
    "marche public", "appel offres", "IRVE", "borne recharge",
    "pompe a chaleur", "pompe à chaleur",
]


class LexiaClient:
    """Client API Legifrance + Judilibre avec stockage PostgreSQL."""

    def __init__(self):
        self.enabled = LEXIA_ENABLED and bool(PISTE_CLIENT_ID)
        self._token: Optional[str] = None
        self._token_expires: float = 0
        self._pool: Optional[asyncpg.Pool] = None

    async def initialize(self) -> bool:
        """Initialise les tables PostgreSQL et verifie la connexion PISTE."""
        if not self.enabled:
            logger.info("LEXIA desactive (LEXIA_ENABLED=false ou PISTE_CLIENT_ID manquant)")
            return False

        try:
            self._pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=3)

            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS legal_articles (
                        id SERIAL PRIMARY KEY,
                        source VARCHAR(20) NOT NULL,
                        article_id VARCHAR(100) UNIQUE,
                        title TEXT,
                        content TEXT,
                        metadata JSONB DEFAULT '{}',
                        relevance_tags TEXT[],
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS legal_alerts (
                        id SERIAL PRIMARY KEY,
                        type VARCHAR(20),
                        severity VARCHAR(10) DEFAULT 'info',
                        title TEXT,
                        summary TEXT,
                        impact TEXT,
                        source_url TEXT,
                        source_article_id VARCHAR(100),
                        notified BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_legal_articles_source
                    ON legal_articles(source, updated_at DESC)
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_legal_alerts_notified
                    ON legal_alerts(notified, created_at DESC)
                """)

            # Tester l'obtention d'un token PISTE
            token = await self._get_token()
            if token:
                logger.info("LEXIA initialise — token PISTE obtenu")
                return True
            else:
                logger.warning("LEXIA: impossible d'obtenir un token PISTE")
                return False

        except Exception as e:
            logger.error("LEXIA: erreur initialisation: %s", e)
            return False

    async def _get_token(self) -> Optional[str]:
        """Obtient ou renouvelle le token OAuth2 PISTE (cache)."""
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    PISTE_TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": PISTE_CLIENT_ID,
                        "client_secret": PISTE_CLIENT_SECRET,
                        "scope": "openid",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._token = data.get("access_token")
                    expires_in = data.get("expires_in", 3600)
                    self._token_expires = time.time() + expires_in
                    return self._token
                else:
                    logger.error("PISTE token error %d: %s", resp.status_code, resp.text[:200])
                    return None
        except Exception as e:
            logger.error("PISTE token exception: %s", e)
            return None

    def _headers(self) -> dict:
        """Headers pour les appels API PISTE."""
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # =========================================
    # RECHERCHE DANS LES CODES (Legifrance)
    # =========================================

    async def search_code(self, query: str, code_name: str = "") -> list[dict]:
        """Recherche dans les codes consolides via API Legifrance.

        Args:
            query: Texte a rechercher (ex: "tarif achat photovoltaique")
            code_name: Nom du code (ex: "Code de l'energie"). Vide = tous les codes.
        """
        token = await self._get_token()
        if not token:
            return []

        payload = {
            "recherche": {
                "champs": [
                    {"typeChamp": "ALL", "criteres": [
                        {"typeRecherche": "EXACTE", "valeur": query}
                    ]}
                ],
                "pageNumber": 1,
                "pageSize": 10,
                "sort": "PERTINENCE",
            },
            "fond": "CODE_DATE",
        }
        if code_name:
            payload["recherche"]["filtres"] = [
                {"facette": "NOM_CODE", "valeurs": [code_name]}
            ]

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{LEGIFRANCE_API_URL}/search",
                    headers=self._headers(),
                    json=payload,
                )
                if resp.status_code != 200:
                    logger.error("Legifrance search %d: %s", resp.status_code, resp.text[:300])
                    return []

                data = resp.json()
                results = []
                for item in data.get("results", []):
                    titles = item.get("titles", [])
                    title = titles[0].get("title", "Sans titre") if titles else "Sans titre"
                    article_id = item.get("id", str(uuid4()))

                    result = {
                        "source": "legifrance",
                        "article_id": article_id,
                        "title": title,
                        "content": item.get("highlights", {}).get("ALL", ""),
                        "metadata": {
                            "code": code_name,
                            "nature": item.get("nature", ""),
                            "origin": item.get("origin", ""),
                        },
                    }
                    results.append(result)
                    await self._store_article(result)

                return results

        except Exception as e:
            logger.error("Legifrance search exception: %s", e)
            return []

    async def get_article(self, article_id: str) -> Optional[dict]:
        """Recupere un article complet par son ID Legifrance."""
        token = await self._get_token()
        if not token:
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{LEGIFRANCE_API_URL}/consult/getArticle",
                    headers=self._headers(),
                    json={"id": article_id},
                )
                if resp.status_code != 200:
                    logger.error("Legifrance getArticle %d", resp.status_code)
                    return None

                data = resp.json()
                article = data.get("article", {})
                result = {
                    "source": "legifrance",
                    "article_id": article_id,
                    "title": article.get("titre", ""),
                    "content": article.get("texteHtml", article.get("texte", "")),
                    "metadata": {
                        "etat": article.get("etat", ""),
                        "dateDebut": article.get("dateDebut", ""),
                        "dateFin": article.get("dateFin", ""),
                        "num": article.get("num", ""),
                    },
                }
                await self._store_article(result)
                return result

        except Exception as e:
            logger.error("Legifrance getArticle exception: %s", e)
            return None

    # =========================================
    # RECHERCHE JURISPRUDENCE (Judilibre)
    # =========================================

    async def search_jurisprudence(self, query: str, max_results: int = 5) -> list[dict]:
        """Recherche dans la jurisprudence via API Judilibre."""
        token = await self._get_token()
        if not token:
            return []

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{JUDILIBRE_API_URL}/search",
                    headers=self._headers(),
                    params={
                        "query": query,
                        "page_size": max_results,
                        "sort": "score",
                        "order": "desc",
                    },
                )
                if resp.status_code != 200:
                    logger.error("Judilibre search %d: %s", resp.status_code, resp.text[:300])
                    return []

                data = resp.json()
                results = []
                for item in data.get("results", []):
                    article_id = item.get("id", str(uuid4()))
                    result = {
                        "source": "judilibre",
                        "article_id": f"jud-{article_id}",
                        "title": f"{item.get('jurisdiction', '')} — {item.get('chamber', '')} — {item.get('decision_date', '')}",
                        "content": item.get("text", item.get("summary", "")),
                        "metadata": {
                            "jurisdiction": item.get("jurisdiction", ""),
                            "chamber": item.get("chamber", ""),
                            "decision_date": item.get("decision_date", ""),
                            "solution": item.get("solution", ""),
                            "number": item.get("number", ""),
                            "ecli": item.get("ecli", ""),
                        },
                    }
                    results.append(result)
                    await self._store_article(result)

                return results

        except Exception as e:
            logger.error("Judilibre search exception: %s", e)
            return []

    # =========================================
    # VEILLE JORF (derniers textes ENR)
    # =========================================

    async def check_veille(self, days: int = 30) -> list[dict]:
        """Recherche les derniers textes ENR dans le JORF."""
        token = await self._get_token()
        if not token:
            return []

        all_results = []
        for keyword in VEILLE_KEYWORDS[:5]:  # Limiter pour ne pas surcharger l'API
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    payload = {
                        "recherche": {
                            "champs": [
                                {"typeChamp": "ALL", "criteres": [
                                    {"typeRecherche": "EXACTE", "valeur": keyword}
                                ]}
                            ],
                            "pageNumber": 1,
                            "pageSize": 3,
                            "sort": "DATE_DESC",
                        },
                        "fond": "JORF",
                    }
                    resp = await client.post(
                        f"{LEGIFRANCE_API_URL}/search",
                        headers=self._headers(),
                        json=payload,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for item in data.get("results", []):
                            titles = item.get("titles", [])
                            title = titles[0].get("title", "") if titles else ""
                            if title and title not in [r["title"] for r in all_results]:
                                all_results.append({
                                    "source": "jorf",
                                    "title": title,
                                    "keyword": keyword,
                                    "date": item.get("lastModifiedDate", ""),
                                    "id": item.get("id", ""),
                                })
            except Exception as e:
                logger.warning("Veille JORF %s: %s", keyword, e)
                continue

        return all_results

    # =========================================
    # STOCKAGE POSTGRESQL
    # =========================================

    async def _store_article(self, article: dict) -> bool:
        """Stocke un article dans legal_articles (upsert)."""
        if not self._pool:
            return False

        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO legal_articles (source, article_id, title, content, metadata, updated_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    ON CONFLICT (article_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                """,
                    article.get("source", ""),
                    article.get("article_id", str(uuid4())),
                    article.get("title", ""),
                    article.get("content", ""),
                    article.get("metadata", {}),
                )
            return True
        except Exception as e:
            logger.error("Store article error: %s", e)
            return False

    async def create_alert(
        self, type_: str, severity: str, title: str, summary: str,
        impact: str = "", source_url: str = ""
    ) -> bool:
        """Cree une alerte juridique."""
        if not self._pool:
            return False

        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO legal_alerts (type, severity, title, summary, impact, source_url)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, type_, severity, title, summary, impact, source_url)
            return True
        except Exception as e:
            logger.error("Create alert error: %s", e)
            return False

    async def get_unnotified_alerts(self) -> list[dict]:
        """Recupere les alertes non encore notifiees."""
        if not self._pool:
            return []

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, type, severity, title, summary, impact, source_url, created_at
                    FROM legal_alerts
                    WHERE notified = FALSE
                    ORDER BY created_at DESC
                    LIMIT 20
                """)
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Get alerts error: %s", e)
            return []

    async def mark_alerts_notified(self, alert_ids: list[int]) -> None:
        """Marque des alertes comme notifiees."""
        if not self._pool or not alert_ids:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE legal_alerts SET notified = TRUE WHERE id = ANY($1)",
                    alert_ids,
                )
        except Exception as e:
            logger.error("Mark alerts error: %s", e)

    async def close(self):
        """Ferme le pool PostgreSQL."""
        if self._pool:
            await self._pool.close()


# =========================================
# FORMATAGE TELEGRAM
# =========================================

def format_legifrance_results(results: list[dict], query: str) -> str:
    """Formate les resultats Legifrance pour Telegram."""
    if not results:
        return f"Aucun resultat pour \"{query}\" dans les codes."

    lines = [f"📜 **Resultats Legifrance** ({len(results)}) :\n"]
    for i, r in enumerate(results[:5], 1):
        title = r.get("title", "Sans titre")[:100]
        content = r.get("content", "")[:200]
        meta = r.get("metadata", {})
        code = meta.get("code", "")
        lines.append(f"**{i}. {title}**")
        if code:
            lines.append(f"   Code : {code}")
        if content:
            lines.append(f"   {content}...")
        lines.append("")

    return "\n".join(lines)


def format_jurisprudence_results(results: list[dict], query: str) -> str:
    """Formate les resultats Judilibre pour Telegram."""
    if not results:
        return f"Aucune jurisprudence trouvee pour \"{query}\"."

    lines = [f"⚖️ **Jurisprudence** ({len(results)}) :\n"]
    for i, r in enumerate(results[:5], 1):
        title = r.get("title", "")[:100]
        meta = r.get("metadata", {})
        solution = meta.get("solution", "")
        ecli = meta.get("ecli", "")
        number = meta.get("number", "")
        lines.append(f"**{i}. {title}**")
        if solution:
            lines.append(f"   Solution : {solution}")
        if number:
            lines.append(f"   N° {number}")
        if ecli:
            lines.append(f"   ECLI : {ecli}")
        content = r.get("content", "")[:150]
        if content:
            lines.append(f"   {content}...")
        lines.append("")

    return "\n".join(lines)


def format_veille_results(results: list[dict]) -> str:
    """Formate les resultats de veille JORF pour Telegram."""
    if not results:
        return "Aucun nouveau texte ENR dans le JORF recent."

    lines = ["📰 **Veille juridique ENR** :\n"]
    for i, r in enumerate(results[:10], 1):
        title = r.get("title", "")[:120]
        keyword = r.get("keyword", "")
        date = r.get("date", "")
        lines.append(f"**{i}.** {title}")
        if date:
            lines.append(f"   Date : {date}")
        lines.append(f"   Mot-cle : {keyword}")
        lines.append("")

    return "\n".join(lines)
