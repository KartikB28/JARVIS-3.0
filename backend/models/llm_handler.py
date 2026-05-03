"""
Ollama LLM handler.

CHAPPIE uses the LLM as a *translator* only — to clarify ambiguous user
intent or to format edge-case responses.  All actual decision-making and
execution happens in pure Python (see core/).
"""

from typing import Dict, List, Optional

import aiohttp

from utils.logger import setup_logger

logger = setup_logger(__name__)


class OllamaHandler:
    """Async HTTP client for a local Ollama server."""

    def __init__(self, config: Dict):
        cfg = config.get("llm", {})
        self.url = cfg.get("ollama_url", "http://localhost:11434").rstrip("/")
        self.model = cfg.get("model", "mistral")
        self.temperature = cfg.get("temperature", 0.2)
        self.timeout = cfg.get("timeout", 60)
        self.default_system = config.get("llm_system_prompt", "")

    async def generate(
        self,
        messages: List[Dict],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Send a chat completion request to Ollama and return the text."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
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

    async def health(self) -> bool:
        """Check whether the Ollama server is reachable."""
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.url}/api/tags") as resp:
                    return resp.status == 200
        except Exception:
            return False
