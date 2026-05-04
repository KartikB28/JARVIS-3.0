"""
Multi-provider LLM handler.

CHAPPIE talks to one of:
  - Ollama (local, default — http://localhost:11434)
  - Any OpenAI-compatible chat-completions endpoint:
      Kimi K2 (Moonshot)        https://api.moonshot.ai/v1
      Qwen   (DashScope intl)   https://dashscope-intl.aliyuncs.com/compatible-mode/v1
      OpenAI                    https://api.openai.com/v1
      OpenRouter                https://openrouter.ai/api/v1
      DeepSeek                  https://api.deepseek.com/v1
      LM Studio (local)         http://localhost:1234/v1
      vLLM / TGI                http://localhost:8000/v1

Provider is selected via config.llm.provider — "ollama" or "openai_compatible".
API keys come from env vars (preferred) or directly from config (fallback).

The LLM is still used as a *translator/composer*. All decision logic stays
in Python; we only call the LLM for ambiguous planning, skill text generation,
and freeform conversation.
"""

import os
from typing import Dict, List, Optional

import aiohttp

from utils.logger import setup_logger

logger = setup_logger(__name__)


class LLMHandler:
    """Unified LLM handler with provider routing."""

    def __init__(self, config: Dict):
        cfg = config.get("llm", {})
        self.provider: str = cfg.get("provider", "ollama").lower()
        self.model: str = cfg.get("model", "mistral")
        self.temperature: float = cfg.get("temperature", 0.3)
        self.timeout: int = cfg.get("timeout", 60)
        self.default_system: str = config.get("llm_system_prompt", "")

        if self.provider == "ollama":
            self.url = cfg.get("ollama_url", "http://localhost:11434").rstrip("/")
            self.api_key = None
        else:
            self.url = cfg.get("base_url", "").rstrip("/")
            env_key = cfg.get("api_key_env", "LLM_API_KEY")
            self.api_key = os.environ.get(env_key) or cfg.get("api_key", "")
            if not self.api_key:
                logger.warning(
                    f"No API key for provider={self.provider}; set ${env_key} "
                    "or llm.api_key in config.json. Falling back to empty key."
                )

        logger.info(f"LLM provider={self.provider} model={self.model}")

    async def generate(
        self,
        messages: List[Dict],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Send a chat completion. Returns text or empty string on failure."""
        if self.provider == "ollama":
            return await self._generate_ollama(messages, system, temperature)
        return await self._generate_openai_compatible(messages, system, temperature)

    async def health(self) -> bool:
        """Best-effort reachability check."""
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if self.provider == "ollama":
                    async with session.get(f"{self.url}/api/tags") as resp:
                        return resp.status == 200
                # Most OpenAI-compatible endpoints respond on /models
                headers = (
                    {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                )
                async with session.get(f"{self.url}/models", headers=headers) as resp:
                    return resp.status in (200, 401)  # 401 = reachable, just unauthed
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Ollama
    # ------------------------------------------------------------------

    async def _generate_ollama(
        self,
        messages: List[Dict],
        system: Optional[str],
        temperature: Optional[float],
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": (
                    self.temperature if temperature is None else temperature
                ),
            },
        }
        sys_prompt = system if system is not None else self.default_system
        if sys_prompt:
            payload["system"] = sys_prompt

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.url}/api/chat", json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return data.get("message", {}).get("content", "").strip()
        except aiohttp.ClientError as exc:
            logger.error(f"Ollama request failed: {exc}")
            return ""
        except Exception as exc:
            logger.error(f"Unexpected LLM error: {exc}")
            return ""

    # ------------------------------------------------------------------
    # OpenAI-compatible (Kimi, Qwen, OpenAI, OpenRouter, DeepSeek, ...)
    # ------------------------------------------------------------------

    async def _generate_openai_compatible(
        self,
        messages: List[Dict],
        system: Optional[str],
        temperature: Optional[float],
    ) -> str:
        full: List[Dict] = []
        sys_prompt = system if system is not None else self.default_system
        if sys_prompt:
            full.append({"role": "system", "content": sys_prompt})
        full.extend(messages)

        payload = {
            "model": self.model,
            "messages": full,
            "temperature": (
                self.temperature if temperature is None else temperature
            ),
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.url}/chat/completions", json=payload, headers=headers
                ) as resp:
                    if resp.status == 401:
                        logger.error(
                            "LLM 401 unauthorized — check your API key "
                            f"(provider={self.provider})"
                        )
                        return ""
                    resp.raise_for_status()
                    data = await resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        logger.warning(f"LLM returned no choices: {data}")
                        return ""
                    return (
                        choices[0].get("message", {}).get("content", "") or ""
                    ).strip()
        except aiohttp.ClientError as exc:
            logger.error(f"LLM request failed: {exc}")
            return ""
        except Exception as exc:
            logger.error(f"Unexpected LLM error: {exc}")
            return ""


# Backwards-compatible alias — the rest of the codebase imports OllamaHandler.
OllamaHandler = LLMHandler
