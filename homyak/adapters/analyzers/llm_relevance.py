"""Analyzer stage 7: LLM-судья — оценивает релевантность item'а профилю интересов (0..1).

Кэш по scored_profile_version: переоценивает только при смене профиля или новом item'е.
Пишет ctx.llm_relevance/llm_reason и на ORM-item (персистится processor'ом). Best-effort.
"""

from __future__ import annotations

import structlog

from homyak.core.interfaces import AnalyzerContext
from homyak.core.llm import OllamaLLM

log = structlog.get_logger(__name__)

_SYSTEM = (
    "Ты — персональный фильтр новостей. Оцени, насколько новость релевантна читателю "
    "СТРОГО по его профилю интересов. Верни строго JSON "
    '{"score": <число 0..1>, "reason": "<кратко почему>", "matched": ["<интерес>", ...]}. '
    "score=1 — точно в интересах; 0 — совсем мимо; учитывай явные «не интересно»."
)


class LlmRelevanceAnalyzer:
    name = "llm_relevance"
    stage = 7

    def __init__(self, repo, llm: OllamaLLM | None = None) -> None:
        self._repo = repo
        self._llm = llm or OllamaLLM()

    async def analyze(self, ctx: AnalyzerContext) -> None:
        profile = await self._repo.get_active_profile()
        if profile is None:
            return  # нет профиля — судью пропускаем
        version, description, topics = profile
        item = ctx.item

        # кэш: уже оценён против этой версии профиля
        if item.llm_relevance is not None and item.scored_profile_version == version:
            ctx.llm_relevance = item.llm_relevance
            ctx.llm_reason = item.llm_reason
            return

        loves = [t["name"] for t in topics if t.get("polarity") in ("love", "like")]
        avoids = [t["name"] for t in topics if t.get("polarity") in ("dislike", "mute")]
        tags = ctx.tags or list(item.tags or [])
        user = (
            f"Профиль читателя: «{description}»\n"
            f"Явно интересно: {', '.join(loves) or '—'}\n"
            f"Не интересно: {', '.join(avoids) or '—'}\n\n"
            f"Новость:\nЗаголовок: {item.title or ''}\n"
            f"Текст: {(item.text or '')[:1500]}\nТеги: {', '.join(tags) or '—'}"
        )
        try:
            data = await self._llm.chat_json(_SYSTEM, user)
        except Exception as e:  # best-effort
            log.warning("llm_relevance_failed", item=ctx.item_id, error=str(e))
            return

        try:
            score = float(data.get("score"))
        except (TypeError, ValueError):
            return
        score = max(0.0, min(1.0, score))
        reason = data.get("reason") if isinstance(data.get("reason"), str) else None

        ctx.llm_relevance = score
        ctx.llm_reason = reason
        item.llm_relevance = score
        item.llm_reason = reason
        item.scored_profile_version = version
