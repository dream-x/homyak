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

import time

import structlog
from sqlalchemy import text

from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.core.config import settings
from homyak.core.interests import weights as interest_weights
from homyak.core.interfaces import AnalyzerContext
from homyak.core.scoring import cosine

log = structlog.get_logger(__name__)

_REFS_TTL = 60.0  # сек: как часто сверять версии профилей
_REFS_RETRY = 5.0  # сек: пауза перед повтором после ошибки


class PrefilterAnalyzer:
    name = "prefilter"
    stage = 3  # ПО НОМЕРУ: после watchlist(2)/embedder(2), строго до llm_tagger(4)

    def __init__(self, qdrant, embedder: EmbedderAnalyzer | None = None) -> None:
        # qdrant тут нужен только чтобы собрать эмбеддер: сам гейт в Qdrant не ходит —
        # он сравнивает вектор айтема с векторами профилей, которые держит у себя в _refs.
        self._emb = embedder or EmbedderAnalyzer(qdrant)
        self._refs: dict[str, list[float]] | None = None  # вектора профилей, лениво
        self._sig: tuple = ()  # (вертикаль, версия профиля) — для инвалидации кэша
        self._refs_at: float = 0.0

    async def _refs_vectors(self, session) -> dict[str, list[float]]:
        """Опорные вектора = активные профили. Кэш с TTL + инвалидация по версии профиля.

        Профиль меняется в рантайме (🔇 mute → set_profile, refine, CLI), а гейт по старому
        вектору начал бы резать новые темы — и это НЕВОССТАНОВИМО: пути переобработки
        skipped-айтемов не существует. Поэтому сверяем версии, а не кэшируем навсегда.
        """
        now = time.monotonic()
        if self._refs is not None and (now - self._refs_at) < _REFS_TTL:
            return self._refs
        try:
            rows = (
                await session.execute(
                    text(
                        "select vertical, version, description, topics::text"
                        " from profile where active order by vertical"
                    )
                )
            ).all()
            sig = tuple((r[0], r[1]) for r in rows)
            if self._refs is not None and sig == self._sig:
                self._refs_at = now  # профили не менялись — продлеваем кэш без переэмбеддинга
                return self._refs
            refs = {v: await self._emb._embed(f"{d} {t}") for v, _ver, d, t in rows}
            self._refs, self._sig, self._refs_at = refs, sig, now
            log.info("prefilter_refs_ready", verticals=list(refs), versions=sig)
        except Exception as e:
            # НЕ кэшируем провал: иначе одна моргнувшая ошибка (Ollama жонглирует моделями,
            # embed ловит таймаут) выключала бы гейт навсегда до рестарта процесса.
            log.warning("prefilter_refs_failed", error=str(e))
            self._refs_at = now - _REFS_TTL + _REFS_RETRY  # ретрай скоро, но не на каждом айтеме
        return self._refs or {}

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

        if best < interest_weights().prefilter_min_sim:
            ctx.skip = True
            ctx.skip_reason = f"низкая близость к профилям: {best:.2f} (ближе всего {best_v})"
