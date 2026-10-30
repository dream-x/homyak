"""Analyzer stage 3: semantic-дедупликация по эмбеддингам.

Ищет в Qdrant похожие items (cosine >= threshold); если у похожего есть кластер — присоединяет
текущий item к нему (мержит кластеры). Защита от гонок — pg_advisory_xact_lock на целевой кластер.
Читает ctx.embedding от embedder'а (без повторного запроса в Qdrant).

Пределы мержа (ревью 31.10.2026): хит засчитывается только в окне по времени, а слияние — только
если РЕЗУЛЬТАТ (цель + переносимые + текущий) не превышает потолка. Всё, что влияет на решение,
перечитывается под локом: принадлежность хита (split мог увести его в другой кластер между чтением
и локом) и фактическое число членов (Cluster.size исторически расходился с реальностью).
Членство текущего item'а пишется в ту же транзакцию, что и перенос, а не отдельным
mark_processed'ом позже — иначе split между ними не видит присоединяемую запись.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import Cluster, NewsItem
from homyak.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def within_window(
    item_published: datetime | None, hit_published: str | datetime | None, max_age_hours: int
) -> bool:
    """Похожий item попадает в ту же историю, только если они близки по времени.

    Кластер 1400 («Запись @seeallochnaya», 926 записей из 63 лент за три месяца) слипся именно из
    шаблонных постов, похожих по вектору, но никак не по событию. Нет даты у одного из двух —
    проверять нечем, пропускаем (как и при max_age_hours=0). Это окно между item и хитом, а не
    гарантия длительности всего кластера: цепочка соседей может растянуться дольше — её держит
    потолок размера.
    """
    if max_age_hours <= 0 or item_published is None or not hit_published:
        return True
    if isinstance(hit_published, str):
        try:
            other = datetime.fromisoformat(hit_published)
        except ValueError:
            return True
    else:
        other = hit_published
    return abs(_aware(item_published) - _aware(other)) <= timedelta(hours=max_age_hours)


def cluster_full(size: int, max_cluster: int) -> bool:
    """Реальная история редко собирает больше нескольких десятков записей; дальше — склейка."""
    return max_cluster > 0 and size >= max_cluster


async def _members(s: AsyncSession, cluster_id: int, *, exclude_id: int | None = None) -> int:
    """Фактическое число членов кластера (не Cluster.size: тот расходился с реальностью)."""
    q = select(func.count()).select_from(NewsItem).where(NewsItem.cluster_id == cluster_id)
    if exclude_id is not None:
        q = q.where(NewsItem.id != exclude_id)
    return int(await s.scalar(q) or 0)


class SimilarityDedupAnalyzer:
    name = "similarity_dedup"
    stage = 3

    def __init__(
        self,
        qdrant: QdrantStore,
        threshold: float | None = None,
        *,
        max_age_hours: int | None = None,
        max_cluster: int | None = None,
    ) -> None:
        self._q = qdrant
        self._threshold = threshold if threshold is not None else settings.similarity_threshold
        self._max_age_hours = (
            max_age_hours if max_age_hours is not None else settings.similarity_max_age_hours
        )
        self._max_cluster = (
            max_cluster if max_cluster is not None else settings.similarity_max_cluster
        )

    async def _cluster_of(self, s: AsyncSession, item_id: int) -> int | None:
        found = await s.scalar(select(NewsItem.cluster_id).where(NewsItem.id == item_id))
        return int(found) if found is not None else None

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
        chosen_hit: int | None = None
        for hid, _score, payload in hits:  # hits уже отсортированы по score desc
            cluster = id_to_cluster.get(hid)
            if cluster is None:
                continue
            if not within_window(
                ctx.item.published_at, (payload or {}).get("published_at"), self._max_age_hours
            ):
                log.info("similarity_merge_skipped", item=ctx.item_id, hit=hid, reason="too_old")
                continue
            target, chosen_hit = int(cluster), int(hid)
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

        # Под локом решение принимается заново по фактическим данным: хит мог переехать (split),
        # целевой кластер — исчезнуть (встречный мерж), а счётчик size — врать.
        if await self._cluster_of(s, chosen_hit) != target:
            log.info(
                "similarity_merge_skipped", item=ctx.item_id, into_cluster=target,
                reason="hit_moved",
            )
            return
        if await s.scalar(select(Cluster.id).where(Cluster.id == target)) is None:
            return

        target_members = await _members(s, target, exclude_id=ctx.item_id)
        moved = await _members(s, old, exclude_id=ctx.item_id) if old is not None else 0
        result = target_members + moved + 1  # + текущий item
        if cluster_full(result, self._max_cluster):
            log.info(
                "similarity_merge_skipped", item=ctx.item_id, into_cluster=target,
                reason=f"cluster_full:{target_members}+{moved}+1",
            )
            return

        ctx.cluster_id = target
        ctx.item.cluster_id = target  # членство — в этой же транзакции, не в mark_processed

        if old is not None and old != target:
            if moved:
                await s.execute(
                    update(NewsItem).where(NewsItem.cluster_id == old).values(cluster_id=target)
                )
            await s.execute(delete(Cluster).where(Cluster.id == old))

        await s.execute(update(Cluster).where(Cluster.id == target).values(size=result))
        log.info("similarity_merge", item=ctx.item_id, into_cluster=target, moved=moved, size=result)
