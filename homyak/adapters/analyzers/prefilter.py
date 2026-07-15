"""Analyzer stage 3: гейт предобработки — отсекает шум ДО дорогих LLM-стадий.

Зачем: теггер и судья дёргают LLM на КАЖДЫЙ айтем, включая явный мусор («Form 4 Accel
Entertainment», инсайдерские сделки, тикерная канцелярия). Позиция пользователя: источники
не трогаем, но «точечно понимать что там и выкидывать если надо».

Два уровня:
1. whitelist — есть watch_topics (Iran/Нефть/Claude/…) → пропускаем ВСЕГДА.
   Важно: в замере твит про заморозку активов Ирана дал близость 0.372 — семантика бы его
   выкинула, а это watchlist-тема. Поэтому whitelist идёт первым и безусловен.
2. семантика — max(cos) эмбеддинга айтема к активным профилям < порога → skip.
   Замер на живых данных: мусор 0.23–0.37, содержательное 0.49–0.60 (между ними пустой зазор).

Безопасные дефолты: нет эмбеддинга или нет профилей → пропускаем (лучше лишний LLM-вызов,
чем молча потерянная новость).
"""

from __future__ import annotations

import structlog
from sqlalchemy import text

from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.core.scoring import cosine

log = structlog.get_logger(__name__)


class PrefilterAnalyzer:
    name = "prefilter"
    stage = 3  # после embedder(2)/similarity_dedup(3), до llm_tagger(4)

    def __init__(self, qdrant, embedder: EmbedderAnalyzer | None = None) -> None:
        self._q = qdrant
        self._emb = embedder or EmbedderAnalyzer(qdrant)
        self._refs: dict[str, list[float]] | None = None  # вектора профилей, лениво

    async def _refs_vectors(self, session) -> dict[str, list[float]]:
        """Опорные вектора = активные профили (описание + темы). Считаем один раз на процесс."""
        if self._refs is not None:
            return self._refs
        self._refs = {}
        try:
            rows = (
                await session.execute(
                    text("select vertical, description, topics::text from profile where active")
                )
            ).all()
            for vertical, desc, topics in rows:
                self._refs[vertical] = await self._emb._embed(f"{desc} {topics}")
            log.info("prefilter_refs_ready", verticals=list(self._refs))
        except Exception as e:  # без опорных векторов гейт просто не режет
            log.warning("prefilter_refs_failed", error=str(e))
            self._refs = {}
        return self._refs

    async def analyze(self, ctx: AnalyzerContext) -> None:
        if not settings.prefilter_enabled:
            return

        item = ctx.item
        if item.watch_topics:  # whitelist: трендовая тема — пропускаем безусловно
            return

        if not ctx.embedding:  # эмбеддера не было → судить не на чем, пропускаем
            return

        refs = await self._refs_vectors(ctx.session)
        if not refs:
            return

        best_v, best = "", -1.0
        for vertical, vec in refs.items():
            c = cosine(ctx.embedding, vec)
            if c > best:
                best_v, best = vertical, c

        if best < settings.prefilter_min_sim:
            ctx.skip = True
            ctx.skip_reason = f"низкая близость к профилям: {best:.2f} (ближе всего {best_v})"
