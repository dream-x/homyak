"""Analyzer stage 8 (финальная): гибридная свёртка personal_score + hard-mute.

Компоненты: llm_relevance (судья), taste_cos (близость к вектору вкуса), tag/source affinity,
freshness. Замьюченные темы → personal_score=NULL (в персональную ленту не попадают).
Дорогой llm_relevance кэшируется (stage 7); лёгкие компоненты считаются здесь.
"""

from __future__ import annotations

import structlog

from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.core.scoring import (
    PersonalWeights,
    cosine,
    freshness,
    personal_score,
    weights_from_settings,
)
from homyak.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)


class PersonalizerAnalyzer:
    name = "personalizer"
    stage = 8

    def __init__(
        self,
        repo,
        qdrant: QdrantStore,
        weights: PersonalWeights | None = None,
        taste_ramp: int | None = None,
    ) -> None:
        self._repo = repo
        self._q = qdrant
        self._weights = weights or weights_from_settings()
        self._taste_ramp = taste_ramp if taste_ramp is not None else settings.taste_ramp

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        vertical = ctx.vertical or item.vertical
        if not vertical:  # вне вертикалей → не в персональной ленте
            ctx.personal_score = None
            item.personal_score = None
            return
        tags = ctx.tags or list(item.tags or [])

        # hard-mute: если тег в замьюченных темах профиля вертикали — вон из ленты
        profile = await self._repo.get_active_profile(vertical)
        if profile is not None:
            muted = {t["name"] for t in profile[2] if t.get("polarity") == "mute"}
            if muted and any(t in muted for t in tags):
                ctx.personal_score = None
                item.personal_score = None
                return

        tag_affs = await self._repo.get_tag_affinities(vertical, tags)
        tag_aff = sum(tag_affs.values()) / len(tag_affs) if tag_affs else 0.0
        source_key = item.feed_name or item.author  # affinity на уровне фида/канала
        source_aff = await self._repo.get_source_affinity(vertical, item.source_type, source_key)

        n_liked = await self._repo.get_taste_n_liked(vertical)
        taste_cos = 0.0
        if n_liked > 0 and ctx.embedding:
            taste = await self._q.get_taste(vertical)
            taste_cos = cosine(ctx.embedding, taste)

        llm_rel = ctx.llm_relevance if ctx.llm_relevance is not None else item.llm_relevance
        ps = personal_score(
            llm_relevance=llm_rel,
            taste_cos=taste_cos,
            tag_aff=tag_aff,
            source_aff=source_aff,
            freshness_val=freshness(item.published_at),
            n_liked=n_liked,
            weights=self._weights,
            taste_ramp=self._taste_ramp,
        )
        ctx.personal_score = ps
        item.personal_score = ps
