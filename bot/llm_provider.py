"""
LLMProvider - Abstraction multi-providers compatible OpenAI
============================================================
Chaine de fallback : DeepSeek V3.2 -> Qwen3-Plus -> Claude Haiku
Tous utilisent le format OpenAI (base_url + api_key).
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger("agea.llm")

# Configuration des providers (tous compatibles format OpenAI)
LLM_CONFIGS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout": 15,
        "cost_per_1k_input": 0.00028,
        "cost_per_1k_output": 0.00042,
    },
    "qwen": {
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "api_key_env": "QWEN_API_KEY",
        "timeout": 15,
        "cost_per_1k_input": 0.00026,
        "cost_per_1k_output": 0.00208,
    },
    "claude": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5-20251001",
        "api_key_env": "ANTHROPIC_API_KEY",
        "timeout": 30,
        "cost_per_1k_input": 0.001,
        "cost_per_1k_output": 0.005,
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3:32b",
        "api_key_env": "",
        "timeout": 60,
        "cost_per_1k_input": 0,
        "cost_per_1k_output": 0,
    },
}

# Ordre de fallback
FALLBACK_CHAIN = ["deepseek", "qwen", "claude"]


class LLMProvider:
    """Gere les appels LLM avec fallback automatique."""

    def __init__(self):
        self.current_provider = os.getenv("LLM_PROVIDER", "deepseek")
        logger.info("LLMProvider initialise - defaut: %s", self.current_provider)

    async def chat(
        self,
        messages: list[dict],
        provider: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ) -> str:
        """
        Envoie un message au LLM avec fallback automatique.

        Args:
            messages: Liste de messages format OpenAI [{"role": "...", "content": "..."}]
            provider: Forcer un provider specifique (ignore le fallback)
            temperature: Temperature de generation
            max_tokens: Nombre max de tokens en sortie

        Returns:
            Le texte de la reponse du LLM
        """
        if provider:
            return await self._call_provider(provider, messages, temperature, max_tokens)

        # Fallback automatique
        chain = FALLBACK_CHAIN.copy()

        # Mettre le provider par defaut en premier
        if self.current_provider in chain:
            chain.remove(self.current_provider)
            chain.insert(0, self.current_provider)

        last_error = None
        for p in chain:
            try:
                result = await self._call_provider(p, messages, temperature, max_tokens)
                if p != self.current_provider:
                    logger.warning("Fallback vers %s (defaut %s indisponible)", p, self.current_provider)
                return result
            except Exception as e:
                logger.warning("Provider %s echoue: %s", p, e)
                last_error = e
                continue

        raise RuntimeError(f"Tous les providers ont echoue. Derniere erreur: {last_error}")

    async def _call_provider(
        self,
        provider: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Appelle un provider specifique au format OpenAI."""
        config = LLM_CONFIGS.get(provider)
        if not config:
            raise ValueError(f"Provider inconnu: {provider}")

        api_key = ""
        if config["api_key_env"]:
            api_key = os.getenv(config["api_key_env"], "")
            if not api_key:
                raise ValueError(f"Cle API manquante: {config['api_key_env']}")

        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": config["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        url = f"{config['base_url']}/chat/completions"

        async with httpx.AsyncClient(timeout=config["timeout"]) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()

            data = response.json()
            content = data["choices"][0]["message"]["content"]

            logger.info(
                "LLM %s OK - tokens: %s/%s",
                provider,
                data.get("usage", {}).get("prompt_tokens", "?"),
                data.get("usage", {}).get("completion_tokens", "?"),
            )

            return content
