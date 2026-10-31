"""Analyzer stage 3: semantic-дедупликация по эмбеддингам + финализация членства в кластере.

Ищет в Qdrant похожие items (cosine >= threshold); если у похожего есть кластер — присоединяет
текущий item к нему (мержит кластеры). Читает ctx.embedding от embedder'а (без повторного
запроса в Qdrant).

Протокол блокировок (ревью #006/#007) — один для всех, кто двигает членство: advisory-лок на
каждый затронутый кластер, по возрастанию id, и только потом строки. Так же работает split
(`NewsRepo.split_cluster`). Отсюда:
- присоединение по URL (url_dedup, стадия 1) только решается, а пишется здесь: под локом
  проверяем, что URL-близнец всё ещё в том кластере (split мог увести), и ставим size по факту;
- хит засчитывается только в окне по времени, а слияние — только если РЕЗУЛЬТАТ (цель +
  переносимые + текущий) не превышает потолка; считаем членов, а не Cluster.size (он врал);
- под локом перечитываем принадлежность хита и существование цели;
- членство текущего item'а — в транзакции стадий, mark_processed его потом не перекрывает;
- FK item→чужой кластер ставится только здесь, под локом: поставленный раньше, он через autoflush
  держал бы строку кластера (KEY SHARE) и не давал встречному мержу удалить её — тот ждал бы нас,
  а мы — его advisory-лок.
Свежий кластер, созданный url_dedup для одинокого item'а, никто не видит — для него лок не нужен.
Повторная обработка (cluster_kept) членство не трогает и не мержит: строка item'а уже видна
split'у, а flush тела на ранних стадиях держит её — лок на кластер после этого = взаимная
блокировка. Это не потеря: кластер у записи уже есть с первого прохода.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, exists, func, select, text, update
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

    async def _pick_target(self, ctx: AnalyzerContext) -> tuple[int | None, int | None]:
        """(кластер лучшего хита в окне, сам хит) по Qdrant — до лока, как подсказка."""
        if ctx.embedding is None:
            return None, None
        hits = await self._q.search_similar(
            ctx.embedding, limit=5, score_threshold=self._threshold, exclude_id=ctx.item_id
        )
        if not hits:
            return None, None
        rows = (
            await ctx.session.execute(
                select(NewsItem.id, NewsItem.cluster_id).where(
                    NewsItem.id.in_([h[0] for h in hits])
                )
            )
        ).all()
        id_to_cluster = {r[0]: r[1] for r in rows}
        for hid, _score, payload in hits:  # hits уже отсортированы по score desc
            cluster = id_to_cluster.get(hid)
            if cluster is None:
                continue
            if not within_window(
                ctx.item.published_at, (payload or {}).get("published_at"), self._max_age_hours
            ):
                log.info("similarity_merge_skipped", item=ctx.item_id, hit=hid, reason="too_old")
                continue
            return int(cluster), int(hid)
        return None, None

    async def _twin_still_in(self, ctx: AnalyzerContext, cluster_id: int) -> bool:
        """URL-близнец, к которому url_dedup присоединил item, всё ещё в этом кластере?"""
        return bool(
            await ctx.session.scalar(
                select(
                    exists().where(
                        NewsItem.url_normalized == ctx.item.url_normalized,
                        NewsItem.id != ctx.item_id,
                        NewsItem.cluster_id == cluster_id,
                    )
                )
            )
        )

    async def _fresh_cluster(self, ctx: AnalyzerContext) -> int:
        cluster = Cluster(representative_id=ctx.item_id, size=1)
        ctx.session.add(cluster)
        await ctx.session.flush()
        ctx.item.cluster_id = cluster.id
        return int(cluster.id)

    async def analyze(self, ctx: AnalyzerContext) -> None:
        s = ctx.session
        if ctx.cluster_kept:
            return  # повторная обработка: членство оставляем как есть (см. докстринг модуля)
        target, chosen_hit = await self._pick_target(ctx)
        old = ctx.cluster_id
        if target == old:
            target = None
        if target is None and not ctx.cluster_joined:
            return  # свой свежий кластер, ни с кем не делимся — лочить нечего

        # Лочим ВСЕ затронутые кластеры разом, по возрастанию id: с параллельной обработкой два
        # item'а могут мержить встречно (A: 1→2, B: 2→1), и лока только на target не хватало —
        # один удалял кластер, на который в этот момент ссылался другой (ForeignKeyViolation).
        # Единый порядок захвата исключает и гонку, и взаимоблокировку.
        for k in sorted({k for k in (old, target) if k is not None}):
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": int(k)})

        # Под локом решения принимаются заново по таблице, не по тому, что читали до него.
        if ctx.cluster_joined and old is not None:
            if await self._twin_still_in(ctx, old):
                ctx.item.cluster_id = old  # FK — только теперь, под локом
            else:
                # split/мерж увёл близнеца между стадией 1 и локом: присоединение устарело, item —
                # в свой кластер (кластер близнеца мы не лочили и под чужой лок не лезем)
                log.info("url_join_stale", item=ctx.item_id, cluster=old)
                old = await self._fresh_cluster(ctx)
                ctx.cluster_joined = False

        final, merged = old, False
        if target is not None:
            if await self._cluster_of(s, chosen_hit) != target or (
                await s.scalar(select(Cluster.id).where(Cluster.id == target)) is None
            ):
                log.info(
                    "similarity_merge_skipped", item=ctx.item_id, into_cluster=target,
                    reason="hit_moved",
                )
            else:
                target_members = await _members(s, target, exclude_id=ctx.item_id)
                moved = await _members(s, old, exclude_id=ctx.item_id) if old is not None else 0
                result = target_members + moved + 1  # + текущий item
                if cluster_full(result, self._max_cluster):
                    log.info(
                        "similarity_merge_skipped", item=ctx.item_id, into_cluster=target,
                        reason=f"cluster_full:{target_members}+{moved}+1",
                    )
                else:
                    if old is not None:
                        if moved:
                            await s.execute(
                                update(NewsItem)
                                .where(NewsItem.cluster_id == old)
                                .values(cluster_id=target)
                            )
                        await s.execute(delete(Cluster).where(Cluster.id == old))
                    await s.execute(
                        update(Cluster).where(Cluster.id == target).values(size=result)
                    )
                    final, merged = target, True
                    log.info(
                        "similarity_merge", item=ctx.item_id, into_cluster=target,
                        moved=moved, size=result,
                    )

        ctx.cluster_id = final
        ctx.item.cluster_id = final  # членство — в этой же транзакции, не в mark_processed
        if not merged and ctx.cluster_joined and final is not None:
            # присоединение по URL: size по факту (url_dedup раньше его не трогал — в проде 2483
            # кластера разошлись со счётом; теперь каждый join подлечивает)
            size = await _members(s, final, exclude_id=ctx.item_id) + 1
            await s.execute(update(Cluster).where(Cluster.id == final).values(size=size))
