"""Обёртка над Ollama /api/chat (JSON и текст) с circuit breaker'ом.

Переиспользуется в Phase 6 (LLM-судья против профиля интересов).
"""

from __future__ import annotations

import json

import httpx
import structlog

from homyak.core.circuit import CircuitBreaker
from homyak.core.config import settings

log = structlog.get_logger(__name__)


class OllamaLLM:
    def __init__(self, model: str | None = None, breaker: CircuitBreaker | None = None) -> None:
        self._model = model or settings.llm_model
        self._breaker = breaker or CircuitBreaker()

    async def _chat(self, system: str, user: str, json_format: bool) -> str:
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.1},
        }
        if json_format:
            payload["format"] = "json"
        async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=180) as client:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return data["message"]["content"]

    async def chat_json(self, system: str, user: str) -> dict:
        content = await self._breaker.call(self._chat, system, user, True)
        return json.loads(content)

    async def chat_text(self, system: str, user: str) -> str:
        return await self._breaker.call(self._chat, system, user, False)
