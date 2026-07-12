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
    def __init__(
        self,
        model: str | None = None,
        fallback: str | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._model = model or settings.llm_model
        self._fallback = fallback  # запасная модель при сбое основной
        self._breaker = breaker or CircuitBreaker()

    async def _chat(
        self,
        system: str,
        user: str,
        json_format: bool,
        think: bool | None = None,
        model: str | None = None,
    ) -> str:
        payload: dict = {
            "model": model or self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.1},
        }
        if json_format:
            payload["format"] = "json"
        if think is not None:  # для thinking-моделей (qwen3): отключаем reasoning
            payload["think"] = think
        async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=300) as client:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return data["message"]["content"]

    async def chat_json(self, system: str, user: str) -> dict:
        content = await self._with_fallback(system, user, True, None)
        return json.loads(content)

    async def chat_text(self, system: str, user: str, think: bool | None = None) -> str:
        return await self._with_fallback(system, user, False, think)

    async def _with_fallback(
        self, system: str, user: str, json_format: bool, think: bool | None
    ) -> str:
        try:
            return await self._breaker.call(self._chat, system, user, json_format, think)
        except Exception as e:
            if not self._fallback:
                raise
            log.warning("llm_fallback", primary=self._model, fallback=self._fallback, error=str(e))
            return await self._chat(system, user, json_format, think, model=self._fallback)
