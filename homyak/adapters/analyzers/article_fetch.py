"""Analyzer stage 0: скачивает полный текст статьи по URL, если RSS дал только огрызок.

Кладёт полный текст в item.text (перезаписывает огрызок) — дальше эмбеддер/теги/саммари/судья
и просмотрщик в Telegram работают уже с полной статьёй. Best-effort, не блокирует пайплайн.
"""

from __future__ import annotations

import structlog

from homyak.core.article import fetch_page
from homyak.core.interfaces import AnalyzerContext

log = structlog.get_logger(__name__)


class ArticleFetchAnalyzer:
    name = "article_fetch"
    stage = 0

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        url = item.url
        if not url or item.source_type == "telegram":
            return
        # у полноконтентных фидов текст уже есть — не дёргаем сеть зря
        if item.text and len(item.text) > 1200:
            return
        # За твитом идём, только если текста нет вовсе: у записей из RSSHub он уже есть,
        # а зеркало API — чужой публичный сервис, и сотни твиттер-источников его положат.
        bare = len((item.text or "").strip()) < 200
        text, title = await fetch_page(url, allow_social=bare)
        if text and len(text) > len(item.text or ""):
            item.text = text
            log.info("article_fetched", item=ctx.item_id, chars=len(text))
        # Заголовок ставим, только если источник его не дал: og:title — это как страницу
        # назвал автор, и он честнее сгенерированного title_gen'ом из текста.
        if title and not (item.title or "").strip():
            item.title = title[:500]
            log.info("article_title_from_meta", item=ctx.item_id, title=title[:80])
