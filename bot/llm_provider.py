"""
LLMProvider - Abstraction multi-providers compatible OpenAI
============================================================
Chaine de fallback : DeepSeek -> Qwen -> Claude
DeepSeek/Qwen utilisent le format OpenAI.
Claude utilise l'API native Anthropic /v1/messages.
"""

import os
import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("agea.llm")

MAX_RETRIES = 1  # 1 retry sur timeout/erreur transitoire

# Configuration des providers (tous compatibles format OpenAI)
LLM_CONFIGS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout": 30,
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

# Valeurs de placeholder a ignorer (evite les faux 401 en fallback)
PLACEHOLDER_KEYS = {
    "",
    "xxx",
    "sk-xxx",
    "sk-ant-xxx",
    "changeme",
    "change-me",
}


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
        attempted = 0

        for p in chain:
            config = LLM_CONFIGS.get(p)
            if not config:
                logger.warning("Provider inconnu dans la chaine: %s", p)
                continue

            if not self._provider_is_configured(p, config):
                logger.info("Provider %s ignore (cle absente/invalide)", p)
                continue

            try:
                attempted += 1
                result = await self._call_with_retry(p, messages, temperature, max_tokens)
                if p != self.current_provider:
                    logger.warning("Fallback vers %s (defaut %s indisponible)", p, self.current_provider)
                return result
            except Exception as e:
                logger.warning("Provider %s echoue: %s", p, e)
                last_error = e
                continue

        if attempted == 0:
            raise RuntimeError(
                "Aucun provider LLM configure correctement (cles API absentes ou placeholders)."
            )

        raise RuntimeError(f"Tous les providers ont echoue. Derniere erreur: {last_error}")

    async def _call_with_retry(
        self,
        provider: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Appelle un provider avec 1 retry sur timeout/erreur transitoire."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await self._call_provider(provider, messages, temperature, max_tokens)
            except (httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                if attempt < MAX_RETRIES:
                    logger.info("Provider %s timeout (tentative %d), retry dans 2s...", provider, attempt + 1)
                    await asyncio.sleep(2)
                    continue
                raise
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (502, 503, 429) and attempt < MAX_RETRIES:
                    logger.info("Provider %s erreur %d (tentative %d), retry dans 3s...",
                                provider, e.response.status_code, attempt + 1)
                    await asyncio.sleep(3)
                    continue
                raise

    async def _call_provider(
        self,
        provider: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Appelle un provider specifique."""
        config = LLM_CONFIGS.get(provider)
        if not config:
            raise ValueError(f"Provider inconnu: {provider}")

        api_key = self._get_api_key(provider, config)

        if provider == "claude":
            return await self._call_claude_api(
                config=config,
                api_key=api_key,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

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

    def _provider_is_configured(self, provider: str, config: dict) -> bool:
        """True si le provider est exploitable avec la config actuelle."""
        if provider == "ollama":
            return True

        api_key_env = config.get("api_key_env", "")
        if not api_key_env:
            return True

        api_key = os.getenv(api_key_env, "")
        return not self._is_placeholder_key(api_key)

    @staticmethod
    def _is_placeholder_key(value: str) -> bool:
        """Detecte les valeurs de placeholder pour eviter les faux appels API."""
        if not value:
            return True
        v = value.strip().lower()
        if v in PLACEHOLDER_KEYS:
            return True
        return v.endswith("-xxx")

    def _get_api_key(self, provider: str, config: dict) -> str:
        """Recupere et valide la cle API d'un provider."""
        api_key_env = config.get("api_key_env", "")
        if not api_key_env:
            return ""

        api_key = os.getenv(api_key_env, "")
        if self._is_placeholder_key(api_key):
            raise ValueError(f"Cle API absente/invalide: {api_key_env}")
        return api_key

    async def _call_claude_api(
        self,
        config: dict,
        api_key: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Appel natif Anthropic /v1/messages."""
        system_blocks = []
        anthropic_messages = []

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if not content:
                continue

            if role == "system":
                system_blocks.append(content)
                continue

            if role not in ("user", "assistant"):
                role = "user"

            anthropic_messages.append({
                "role": role,
                "content": content,
            })

        if not anthropic_messages:
            anthropic_messages = [{"role": "user", "content": "Bonjour"}]

        payload = {
            "model": config["model"],
            "messages": anthropic_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_blocks:
            payload["system"] = "\n".join(system_blocks)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

        url = f"{config['base_url']}/messages"
        async with httpx.AsyncClient(timeout=config["timeout"]) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

            content_blocks = data.get("content", [])
            text_parts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

            content = "\n".join(part for part in text_parts if part).strip()
            if not content:
                raise RuntimeError("Reponse Claude vide")

            usage = data.get("usage", {})
            logger.info(
                "LLM claude OK - tokens: %s/%s",
                usage.get("input_tokens", "?"),
                usage.get("output_tokens", "?"),
            )
            return content
