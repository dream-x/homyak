"""Analyzer stage 0: скачивает полный текст статьи по URL, если RSS дал только огрызок.

Кладёт полный текст в item.text (перезаписывает огрызок) — дальше эмбеддер/теги/саммари/судья
и просмотрщик в Telegram работают уже с полной статьёй. Best-effort, не блокирует пайплайн.
"""

from __future__ import annotations

import structlog

from homyak.core.article import fetch_article
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
        text = await fetch_article(url)
        if text and len(text) > len(item.text or ""):
            item.text = text
            log.info("article_fetched", item=ctx.item_id, chars=len(text))
