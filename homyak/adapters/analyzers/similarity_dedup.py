"""Analyzer stage 3: semantic-дедупликация по эмбеддингам.

Ищет в Qdrant похожие items (cosine >= threshold); если у похожего есть кластер — присоединяет
текущий item к нему (мержит кластеры). Защита от гонок — pg_advisory_xact_lock на целевой кластер.
Читает ctx.embedding от embedder'а (без повторного запроса в Qdrant).
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete, func, select, text, update

from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import Cluster, NewsItem
from homyak.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)


class SimilarityDedupAnalyzer:
    name = "similarity_dedup"
    stage = 3

    def __init__(self, qdrant: QdrantStore, threshold: float | None = None) -> None:
        self._q = qdrant
        self._threshold = threshold if threshold is not None else settings.similarity_threshold

    async def analyze(self, ctx: AnalyzerContext) -> None:
        if ctx.embedding is None:
            return
        hits = await self._q.search_similar(
            ctx.embedding,
            limit=5,
            score_threshold=self._threshold,
            exclude_id=ctx.item_id,
        )
        if not hits:
            return

        s = ctx.session
        hit_ids = [h[0] for h in hits]
        rows = (
            await s.execute(
                select(NewsItem.id, NewsItem.cluster_id).where(NewsItem.id.in_(hit_ids))
            )
        ).all()
        id_to_cluster = {r[0]: r[1] for r in rows}

        target: int | None = None
        for hid, _score, _payload in hits:  # hits уже отсортированы по score desc
            cluster = id_to_cluster.get(hid)
            if cluster is not None:
                target = cluster
                break
        if target is None or target == ctx.cluster_id:
            return

        # Сериализуем мержи. Лочим ОБА кластера, и всегда по возрастанию id: с параллельной
        # обработкой пачки два item'а могут мержить встречно (A: 1→2, B: 2→1), и лока только на
        # target не хватало — один удалял кластер, на который в этот момент ссылался другой
        # (ForeignKeyViolation). Единый порядок захвата исключает и гонку, и взаимоблокировку.
        old = ctx.cluster_id
        keys = sorted({int(target), int(old)} if old is not None else {int(target)})
        for k in keys:
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": k})

        # после захвата убеждаемся, что целевой кластер ещё жив — его мог удалить встречный мерж
        if await s.scalar(select(Cluster.id).where(Cluster.id == target)) is None:
            return

        ctx.cluster_id = target

        moved = 0
        if old is not None and old != target:
            moved = (
                await s.scalar(
                    select(func.count()).select_from(NewsItem).where(NewsItem.cluster_id == old)
                )
                or 0
            )
            if moved:
                await s.execute(
                    update(NewsItem).where(NewsItem.cluster_id == old).values(cluster_id=target)
                )
            await s.execute(delete(Cluster).where(Cluster.id == old))

        await s.execute(
            update(Cluster).where(Cluster.id == target).values(size=Cluster.size + 1 + moved)
        )
        log.info("similarity_merge", item=ctx.item_id, into_cluster=target, moved=moved)
