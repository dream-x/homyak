"""Разбор очереди на переэмбеддинг.

Очередь — не отдельная таблица, а признак в самой записи: `embedding_version IS NULL` либо
разошедшиеся модель/версия (запрос — `NewsRepo.stale_embedding_ids`). Кто переписывает `text`,
обязан сбросить версию: вектор в Qdrant построен по прежнему тексту и после перезаписи ищет не то.

Один и тот же код гоняют планировщик в sweeper'е (порциями) и `homyak-reembed` (разом).
"""

from __future__ import annotations

import structlog

from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import NewsItem
from homyak.storage.db import SessionFactory
from homyak.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)


async def reembed(ids: list[int], qdrant: QdrantStore | None = None) -> int:
    """Переэмбеддить записи по списку id. Возвращает число обработанных."""
    if not ids:
        return 0
    own = qdrant is None
    store = qdrant or QdrantStore(settings.qdrant_url)
    try:
        await store.ensure_collection()
        embedder = EmbedderAnalyzer(store)
        done = 0
        for item_id in ids:
            async with SessionFactory() as s:
                item = await s.get(NewsItem, item_id)
                if item is None:
                    continue
                ctx = AnalyzerContext(item_id=item_id, item=item, session=s)
                await embedder.analyze(ctx)
                await s.commit()
            done += 1
        return done
    finally:
        if own:
            await store.close()
