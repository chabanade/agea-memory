"""
GraphitiClient - Client pour Graphiti Knowledge Graph
======================================================
Gere l'ingestion d'episodes, la recherche semantique,
et la bi-temporalite dans Neo4j via graphiti-core.

Architecture sidecar asynchrone :
- Le Bot appelle UNIQUEMENT search() et get_entity() (lecture)
- Le Worker appelle add_episode() et correct_fact() (ecriture)
- Flag _available pour degradation gracieuse

Patch DeepSeek : OpenAIGenericClient envoie response_format json_schema
que DeepSeek ne supporte pas. DeepSeekLLMClient injecte le schema dans
le prompt et utilise json_object a la place.
"""

import os
import json
import logging
import typing
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("agea.graphiti")

# --- Configuration ---
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
GRAPHITI_GROUP_ID = "mehdi-agea"
SEMAPHORE_LIMIT = int(os.getenv("SEMAPHORE_LIMIT", "3"))
GRAPHITI_ENABLED = os.getenv("GRAPHITI_ENABLED", "false").lower() == "true"
GRAPHITI_READ_ENABLED = os.getenv("GRAPHITI_READ_ENABLED", "false").lower() == "true"


class DeepSeekLLMClient:
    """
    Wrapper autour de OpenAIGenericClient qui force json_object
    au lieu de json_schema (non supporte par DeepSeek).
    Injecte le schema JSON dans le prompt systeme.
    """

    _patched = False

    @staticmethod
    def patch_openai_generic_client():
        """Monkey-patch OpenAIGenericClient._generate_response pour DeepSeek."""
        if DeepSeekLLMClient._patched:
            return

        try:
            from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
            import openai

            original_generate = OpenAIGenericClient._generate_response

            async def patched_generate(self, messages, response_model=None, max_tokens=16384, model_size=None):
                openai_messages = []
                for m in messages:
                    m.content = self._clean_input(m.content)
                    if m.role == 'user':
                        openai_messages.append({'role': 'user', 'content': m.content})
                    elif m.role == 'system':
                        openai_messages.append({'role': 'system', 'content': m.content})

                try:
                    response_format: dict[str, Any] = {'type': 'json_object'}

                    if response_model is not None:
                        json_schema = response_model.model_json_schema()
                        schema_instruction = (
                            f"\n\nYou MUST respond with valid JSON matching this schema:\n"
                            f"```json\n{json.dumps(json_schema, indent=2)}\n```\n"
                            f"Do NOT include any text outside the JSON object."
                        )
                        if openai_messages and openai_messages[0]['role'] == 'system':
                            openai_messages[0]['content'] += schema_instruction
                        else:
                            openai_messages.insert(0, {
                                'role': 'system',
                                'content': schema_instruction,
                            })

                    # DeepSeek max_tokens: [1, 8192] — cap pour éviter 400
                    effective_max = min(
                        self.max_tokens if hasattr(self, 'max_tokens') and self.max_tokens else 8000,
                        8000,
                    )
                    response = await self.client.chat.completions.create(
                        model=self.model or 'deepseek-chat',
                        messages=openai_messages,
                        temperature=self.temperature,
                        max_tokens=effective_max,
                        response_format=response_format,
                    )
                    result = response.choices[0].message.content or '{}'
                    return json.loads(result)
                except openai.RateLimitError as e:
                    from graphiti_core.llm_client.errors import RateLimitError
                    raise RateLimitError from e
                except Exception as e:
                    logger.error("DeepSeek LLM error: %s", e)
                    raise

            OpenAIGenericClient._generate_response = patched_generate
            DeepSeekLLMClient._patched = True
            logger.info("DeepSeek patch applique a OpenAIGenericClient")
        except Exception as e:
            logger.error("Erreur patch DeepSeek: %s", e)


class GraphitiClient:
    """Client pour le knowledge graph Graphiti/Neo4j."""

    def __init__(self):
        self._graphiti = None
        self._initialized = False
        self._available = False

    @property
    def available(self) -> bool:
        """True si Graphiti est operationnel ET active."""
        return self._available and GRAPHITI_ENABLED

    @property
    def read_enabled(self) -> bool:
        """True si la lecture Graphiti est activee (feature flag)."""
        return self.available and GRAPHITI_READ_ENABLED

    async def initialize(self) -> bool:
        """
        Initialise la connexion Graphiti + Neo4j.
        Retourne True si OK, False si echec.
        """
        if not GRAPHITI_ENABLED:
            logger.info("Graphiti desactive (GRAPHITI_ENABLED=false)")
            return False

        try:
            from graphiti_core import Graphiti
            from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
            from graphiti_core.llm_client.config import LLMConfig
            from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
            from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

            # Patch DeepSeek (json_object au lieu de json_schema)
            DeepSeekLLMClient.patch_openai_generic_client()

            # Config LLM partagee (DeepSeek via OpenAI-compatible API)
            llm_config = LLMConfig(
                api_key=DEEPSEEK_API_KEY,
                model="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                temperature=0,
                max_tokens=8192,
            )

            llm_client = OpenAIGenericClient(config=llm_config)

            # Embeddings : Gemini (gratuit, 1 appel par search)
            embedder = GeminiEmbedder(
                config=GeminiEmbedderConfig(
                    api_key=GOOGLE_API_KEY,
                    embedding_model="gemini-embedding-001",
                ),
            )

            # Cross-encoder/Reranker : DeepSeek (meme config que LLM)
            cross_encoder = OpenAIRerankerClient(config=llm_config)

            self._graphiti = Graphiti(
                uri=NEO4J_URI,
                user=NEO4J_USER,
                password=NEO4J_PASSWORD,
                llm_client=llm_client,
                embedder=embedder,
                cross_encoder=cross_encoder,
                store_raw_episode_content=True,
                max_coroutines=SEMAPHORE_LIMIT,
            )

            # Creer les index et contraintes Neo4j
            await self._graphiti.build_indices_and_constraints()

            self._initialized = True
            self._available = True
            logger.info(
                "Graphiti initialise - Neo4j: %s, LLM: DeepSeek, Semaphore: %d",
                NEO4J_URI, SEMAPHORE_LIMIT,
            )
            return True

        except Exception as e:
            logger.error("Echec initialisation Graphiti: %s", e)
            self._available = False
            return False

    # === ECRITURE (WORKER ONLY — jamais appele par le Bot directement) ===

    async def add_episode(
        self,
        content: str,
        source_description: str = "Message Telegram",
        reference_time: Optional[datetime] = None,
        group_id: str = GRAPHITI_GROUP_ID,
    ) -> bool:
        """
        Ingere un episode dans le knowledge graph.
        Graphiti extrait automatiquement les entites et relations.
        Si un fait contradictoire existe, l'ancien est invalide.
        """
        if not self._available:
            return False

        try:
            from graphiti_core.nodes import EpisodeType

            ref_time = reference_time or datetime.now()
            name = f"msg_{ref_time.strftime('%Y%m%d_%H%M%S')}_{id(content) % 10000:04d}"

            await self._graphiti.add_episode(
                name=name,
                episode_body=content,
                source=EpisodeType.text,
                source_description=source_description,
                reference_time=ref_time,
                group_id=group_id,
            )

            logger.info("Episode ingere: %s...", content[:60])
            return True

        except Exception as e:
            logger.error("Erreur ingestion episode: %s", e)
            if self._is_connection_error(e):
                self._available = False
            return False

    async def correct_fact(
        self,
        correction: str,
        source_description: str = "Correction utilisateur",
    ) -> bool:
        """
        Corrige un fait en ingerant la correction comme episode.
        Graphiti detecte la contradiction et invalide l'ancien fait.
        """
        return await self.add_episode(
            content=correction,
            source_description=source_description,
        )

    # === LECTURE (Bot OK — 0 appels LLM, sub-second) ===

    async def search(
        self,
        query: str,
        num_results: int = 5,
        group_id: str = GRAPHITI_GROUP_ID,
    ) -> list[dict]:
        """
        Recherche hybride dans le knowledge graph.
        0 appels LLM (seulement 1 embedding) = sub-second.
        """
        if not self._available:
            return []

        try:
            results = await self._graphiti.search(
                query=query,
                num_results=num_results,
                group_ids=[group_id],
            )

            facts = []
            for edge in results:
                facts.append({
                    "uuid": edge.uuid,
                    "fact": edge.fact,
                    "name": edge.name,
                    "source_node_uuid": edge.source_node_uuid,
                    "target_node_uuid": edge.target_node_uuid,
                    "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                    "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
                    "created_at": edge.created_at.isoformat() if edge.created_at else None,
                    "episodes": edge.episodes if hasattr(edge, "episodes") else [],
                })

            logger.info("Recherche Graphiti '%s': %d faits", query[:30], len(facts))
            return facts

        except Exception as e:
            logger.error("Erreur recherche Graphiti: %s", e)
            if self._is_connection_error(e):
                self._available = False
            return []

    async def get_entity(
        self,
        entity_name: str,
        group_id: str = GRAPHITI_GROUP_ID,
    ) -> Optional[dict]:
        """
        Recupere les faits lies a une entite specifique.
        """
        if not self._available:
            return None

        try:
            results = await self._graphiti.search(
                query=entity_name,
                num_results=15,
                group_ids=[group_id],
            )

            if not results:
                return None

            # Filtrer les faits mentionnant cette entite dans le fact text
            related_facts = []
            name_lower = entity_name.lower()
            for edge in results:
                fact_lower = (edge.fact or "").lower()
                if name_lower in fact_lower:
                    related_facts.append({
                        "fact": edge.fact,
                        "name": edge.name,
                        "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                        "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
                    })

            return {
                "entity": entity_name,
                "facts_count": len(related_facts),
                "facts": related_facts,
            }

        except Exception as e:
            logger.error("Erreur get_entity: %s", e)
            return None

    # === MAINTENANCE ===

    async def build_communities(self, group_id: str = GRAPHITI_GROUP_ID) -> bool:
        """Reconstruit les communautes (clustering). Cron quotidien."""
        if not self._available:
            return False

        try:
            await self._graphiti.build_communities(group_id=group_id)
            logger.info("Communautes reconstruites pour %s", group_id)
            return True
        except Exception as e:
            logger.error("Erreur build_communities: %s", e)
            return False

    async def health_check(self) -> dict:
        """Verifie la sante et tente reconnexion si necessaire."""
        if not GRAPHITI_ENABLED:
            return {"status": "disabled", "available": False}

        if not self._initialized:
            return {"status": "not_initialized", "available": False}

        if not self._available:
            logger.info("Tentative reconnexion Graphiti...")
            ok = await self.initialize()
            return {"status": "reconnected" if ok else "unavailable", "available": ok}

        try:
            await self._graphiti.search(
                query="health_check",
                num_results=1,
                group_ids=[GRAPHITI_GROUP_ID],
            )
            return {"status": "healthy", "available": True}
        except Exception as e:
            self._available = False
            return {"status": f"error: {e}", "available": False}

    async def close(self):
        """Ferme la connexion Neo4j."""
        if self._graphiti:
            try:
                await self._graphiti.close()
                logger.info("Connexion Graphiti fermee")
            except Exception as e:
                logger.warning("Erreur fermeture Graphiti: %s", e)

    # === UTILITAIRES INTERNES ===

    @staticmethod
    def _is_connection_error(error: Exception) -> bool:
        """Detecte les erreurs de connexion pour basculer en mode degrade."""
        error_str = str(error).lower()
        return any(kw in error_str for kw in [
            "connection", "refused", "timeout", "unavailable",
            "neo4j", "socket", "reset",
        ])
