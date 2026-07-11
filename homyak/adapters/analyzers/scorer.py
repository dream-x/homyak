"""Analyzer stage 6: базовый скор (свежесть · внешняя оценка · размер кластера).

В Phase 6 заменяется персональным ранкером под интересы. Всегда считается (не зависит от LLM).
"""

from __future__ import annotations

from sqlalchemy import select

from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import Cluster
from homyak.core.scoring import base_score


class ScorerAnalyzer:
    name = "scorer"
    stage = 6

    async def analyze(self, ctx: AnalyzerContext) -> None:
        size = 1
        if ctx.cluster_id is not None:
            size = (
                await ctx.session.scalar(
                    select(Cluster.size).where(Cluster.id == ctx.cluster_id)
                )
                or 1
            )
        ctx.score = base_score(
            published_at=ctx.item.published_at,
            raw_score=ctx.item.raw_score,
            cluster_size=size,
        )
