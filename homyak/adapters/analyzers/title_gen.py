"""Analyzer stage 3: заголовок для постов без него (Telegram/твиты/RSS-огрызки).

Многие источники не дают title — в ленте/канале/пуше они висят как «—». Выводим заголовок
из первой строки текста (см. core.titles.derive_title). Мутирует item.title напрямую —
processor персистит это на общем commit'е (как и cluster_id). Работает ПОСЛЕ prefilter:
отсеянным заголовок ни к чему.
"""

from __future__ import annotations

from homyak.core.interfaces import AnalyzerContext
from homyak.core.titles import derive_title


class TitleGenAnalyzer:
    name = "title_gen"
    stage = 3  # после prefilter(3) по порядку в registry; строго до summarizer(5)

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        if item.title and item.title.strip():
            return
        # для медиа-постов без текста — мягкий фолбэк по каналу/фиду, не «—»
        fallback = f"Запись {item.feed_name}" if item.feed_name else None
        title = derive_title(item.text, fallback=fallback)
        if title:
            item.title = title
