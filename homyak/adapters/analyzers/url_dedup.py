"""Analyzer stage 1: дедупликация по нормализованному URL.

Ищет существующий кластер с тем же url_normalized; если нашёл — присоединяет item к нему,
иначе создаёт новый кластер с item'ом как representative. Мутирует ctx.cluster_id.
url_normalized уже проставлен репозиторием при upsert'е.

Присоединение к чужому кластеру здесь только РЕШАЕТСЯ (ctx.cluster_joined): счётчик size и
перепроверка, что близнец всё ещё там, — в similarity_dedup под advisory-локом. Трогать строку
Cluster на стадии 1 нельзя: row-lock повис бы до коммита всех стадий, а advisory-лок на тот же
кластер берётся позже (стадия 3) — split, идущий в обратном порядке (advisory → строка),
вставал бы с процессором во взаимную блокировку (ревью #007).
"""

from __future__ import annotations

from sqlalchemy import select

from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import Cluster, NewsItem


class UrlDedupAnalyzer:
    name = "url_dedup"
    stage = 1

    async def analyze(self, ctx: AnalyzerContext) -> None:
        s = ctx.session
        if ctx.item.cluster_id is not None:
            # повторная обработка (resummarize, сброс processed_at): кластер уже есть, не трогаем —
            # иначе item уходил в свежий кластер, а старый оставался со стухшим size
            ctx.cluster_id = int(ctx.item.cluster_id)
            return
        norm = ctx.item.url_normalized

        if norm:
            found = await s.scalar(
                select(NewsItem.cluster_id)
                .where(
                    NewsItem.url_normalized == norm,
                    NewsItem.id != ctx.item_id,
                    NewsItem.cluster_id.isnot(None),
                )
                .limit(1)
            )
            if found is not None:
                ctx.cluster_id = int(found)
                ctx.item.cluster_id = int(found)  # строка item'а — наша, остальное под локом в стадии 3
                ctx.cluster_joined = True
                return

        # Нового кластера ещё нет — создаём с текущим item'ом как representative.
        cluster = Cluster(representative_id=ctx.item_id, size=1)
        s.add(cluster)
        await s.flush()
        ctx.cluster_id = cluster.id
        ctx.item.cluster_id = cluster.id
