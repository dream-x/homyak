"""Analyzer stage 1: дедупликация по нормализованному URL.

Ищет существующий кластер с тем же url_normalized; если нашёл — присоединяет item к нему,
иначе создаёт новый кластер с item'ом как representative. Мутирует ctx.cluster_id.
url_normalized уже проставлен репозиторием при upsert'е.
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
                return

        # Нового кластера ещё нет — создаём с текущим item'ом как representative.
        cluster = Cluster(representative_id=ctx.item_id, size=1)
        s.add(cluster)
        await s.flush()
        ctx.cluster_id = cluster.id
