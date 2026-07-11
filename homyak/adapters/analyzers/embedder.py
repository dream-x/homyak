"""Analyzer stage 2: эмбеддинги bge-m3 через Ollama, upsert вектора в Qdrant.

Пишет ctx.embedding (используется similarity_dedup без повторного запроса) и проставляет
embedding_model/version на item'е. Обёрнут в circuit breaker — при недоступности Ollama
исключение уходит в processor → nak + retry.
"""

from __future__ import annotations

import httpx
import structlog

from homyak.core.circuit import CircuitBreaker
from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)

_MAX_CHARS = 2000  # обрезаем, чтобы не упереться в контекст модели


class EmbedderAnalyzer:
    name = "embedder"
    stage = 2

    def __init__(self, qdrant: QdrantStore, breaker: CircuitBreaker | None = None) -> None:
        self._q = qdrant
        self._breaker = breaker or CircuitBreaker()
        self._model = settings.embedding_model
        self._version = settings.embedding_version

    def _text(self, item) -> str:
        title = item.title or ""
        body = item.text or ""
        combined = f"{title}\n{body}".strip()
        return combined[:_MAX_CHARS] or title or "(empty)"

    async def _embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=60) as client:
            resp = await client.post("/api/embed", json={"model": self._model, "input": text})
            resp.raise_for_status()
            data = resp.json()
        embeddings = data.get("embeddings") or []
        if not embeddings:
            raise RuntimeError("ollama вернул пустой embeddings")
        return embeddings[0]

    async def analyze(self, ctx: AnalyzerContext) -> None:
        vector = await self._breaker.call(self._embed, self._text(ctx.item))
        ctx.embedding = vector
        await self._q.upsert_vector(
            ctx.item_id,
            vector,
            {
                "news_item_id": ctx.item_id,
                "source_type": ctx.item.source_type,
                "category": ctx.item.category,
                "published_at": (
                    ctx.item.published_at.isoformat() if ctx.item.published_at else None
                ),
            },
        )
        ctx.item.embedding_model = self._model
        ctx.item.embedding_version = self._version
