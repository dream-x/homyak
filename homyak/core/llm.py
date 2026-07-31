"""Обёртка над Ollama /api/chat (JSON и текст) с circuit breaker'ом.

Переиспользуется в Phase 6 (LLM-судья против профиля интересов).
"""

from __future__ import annotations

import json

import httpx
import structlog

from homyak.core.circuit import CircuitBreaker
from homyak.core.config import settings
from homyak.core.ollama import targets

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
            # num_ctx явно: у новых моделей дефолт 128K+ раздувает KV-кэш в разы (4B → 13G VRAM)
            "options": {"temperature": 0.1, "num_ctx": settings.llm_num_ctx},
        }
        if json_format:
            payload["format"] = "json"
        if think is not None:  # для thinking-моделей (qwen3): отключаем reasoning
            payload["think"] = think
        last: Exception | None = None
        for url, m in targets(payload["model"]):
            payload["model"] = m
            # облачный фолбэк (ollama.com) требует Bearer; локальный хост — нет
            headers = {}
            if settings.ollama_fallback_key and url == settings.ollama_url_fallback:
                headers["Authorization"] = f"Bearer {settings.ollama_fallback_key}"
            try:
                async with httpx.AsyncClient(base_url=url, timeout=300, headers=headers) as client:
                    resp = await client.post("/api/chat", json=payload)
                    resp.raise_for_status()
                    return resp.json()["message"]["content"]
            except Exception as e:  # хост лёг/недоступен → пробуем запасной с его моделью
                last = e
                log.warning("ollama_host_failed", url=url, model=m, error=str(e)[:120])
        raise last if last else RuntimeError("нет доступных ollama-хостов")

    async def chat_json(self, system: str, user: str) -> dict:
        # think=False обязателен: у thinking-моделей (qwen3.5) reasoning ради JSON-разметки
        # раздувает время с ~1с до ~60с на айтем. Рассуждения тут не нужны.
        content = await self._with_fallback(system, user, True, False)
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
