"""Analyzer stage 8 (финальная): гибридная свёртка personal_score + hard-mute.

Компоненты: llm_relevance (судья), taste_cos (близость к вектору вкуса), tag/source affinity,
freshness. Веса — из config/interests.yaml, там же где сами интересы (см. core/interests.py).
Замьюченные темы → personal_score=NULL + skip_reason (в ленту не попадают, но видны в отсеве).
Дорогой llm_relevance кэшируется (stage 7); лёгкие компоненты считаются здесь.
"""

from __future__ import annotations

import structlog

from homyak.core.interests import MUTE_SKIP_PREFIX, no_boost_topics
from homyak.core.interests import weights as interest_weights
from homyak.core.interfaces import AnalyzerContext
from homyak.core.scoring import (
    PersonalWeights,
    cosine,
    freshness,
    personal_score,
    weights_from_interests,
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
        # None → читаем из config/interests.yaml на каждый айтем. Загрузчик кэширует по mtime,
        # так что это один stat(): правка весов подхватывается без рестарта, как и watch.
        # Явно переданные (тесты) — фиксируем.
        self._weights = weights
        self._taste_ramp = taste_ramp

    async def _muted_tags(self, vertical: str) -> set[str]:
        """Мьюты обоих слоёв: декларация (profile.topics) ∪ кнопка 🔇 (muted_tags).

        Декларацию читаем из БД, а не из interests.yaml: в БД лежит ПРИМЕНЁННАЯ версия
        (её же дословно читает судья), и рантайм не должен зависеть от неприменённых правок файла.
        """
        muted: set[str] = set()
        profile = await self._repo.get_active_profile(vertical)
        if profile is not None:
            # isinstance + t.get("name"): профиль может прийти от LLM (refine) и одна тема без
            # "name" роняла бы КАЖДЫЙ айтем вертикали → mark_failed x5 → вертикаль вставала.
            muted |= {
                t["name"]
                for t in profile[2]
                if isinstance(t, dict) and t.get("name") and t.get("polarity") == "mute"
            }
        muted |= await self._repo.get_muted_tags(vertical)
        return muted

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        vertical = ctx.vertical or item.vertical
        if not vertical:  # вне вертикалей → не в персональной ленте
            ctx.personal_score = None
            item.personal_score = None
            return
        tags = ctx.tags or list(item.tags or [])

        muted = await self._muted_tags(vertical)
        hit = next((t for t in tags if t in muted), None)
        if hit is not None:
            ctx.personal_score = None
            item.personal_score = None
            # Причину пишем в айтем, а НЕ через ctx.skip: на финальной стадии skip заставил бы
            # процессор выбросить уже посчитанные tags/summary/score (см. processor.py:47-51).
            # Раньше причины не было вовсе — мьют убивал айтемы совершенно молча, и `medical`,
            # выкосивший 21% вертикали, прожил незамеченным трое суток. Отсюда же и лог:
            # processor логирует только ctx.skip, так что эта ветка иначе была бы нема.
            item.skip_reason = f"{MUTE_SKIP_PREFIX}{hit}"
            log.info("muted", news_item_id=ctx.item_id, vertical=vertical, tag=hit)
            return
        # Снимаем СВОЮ причину (мьют сняли → `homyak-interests backfill` оживит айтем).
        # Только свою: до stage 8 доходят лишь непрофильтрованные — гейт обрывает конвейер на
        # stage 3 через ctx.skip (processor.py:39-41) — но проверка по префиксу не даёт этому
        # инварианту из чужого файла молча испортить чужую причину, если он изменится.
        if (item.skip_reason or "").startswith(MUTE_SKIP_PREFIX):
            item.skip_reason = None

        tag_affs = await self._repo.get_tag_affinities(vertical, tags)
        tag_aff = sum(tag_affs.values()) / len(tag_affs) if tag_affs else 0.0
        source_key = item.feed_name or item.author  # affinity на уровне фида/канала
        source_aff = await self._repo.get_source_affinity(vertical, item.source_type, source_key)

        n_liked = await self._repo.get_taste_n_liked(vertical)
        taste_cos = 0.0
        if n_liked > 0 and ctx.embedding:
            taste = await self._q.get_taste(vertical)
            taste_cos = cosine(ctx.embedding, taste)

        conf = interest_weights()
        llm_rel = ctx.llm_relevance if ctx.llm_relevance is not None else item.llm_relevance
        ps = personal_score(
            llm_relevance=llm_rel,
            taste_cos=taste_cos,
            tag_aff=tag_aff,
            source_aff=source_aff,
            freshness_val=freshness(item.published_at),
            n_liked=n_liked,
            weights=self._weights or weights_from_interests(),
            taste_ramp=self._taste_ramp if self._taste_ramp is not None else conf.taste_ramp,
        )
        # Пристальное внимание: айтемы вотчлиста бустим (не выше 1.0). Темы с boost:false
        # (напр. «Природные явления») отслеживаем, но в ранге не поднимаем.
        if item.watch_topics and set(item.watch_topics) - no_boost_topics():
            ps = min(1.0, ps + conf.watchlist_boost)
        ctx.personal_score = ps
        item.personal_score = ps
