"""Analyzer stage 2: матч айтема на трендовые темы (секция `watch` в config/interests.yaml).

Без LLM — ключи/синонимы (RU+EN). Ставит ctx.item.watch_topics (персистит processor).
Читает уже дозагруженный article_fetch'ем текст (stage 0), поэтому матч по полному тексту.
"""

from __future__ import annotations

import structlog

from homyak.core.interests import compiled_watchlist
from homyak.core.interfaces import AnalyzerContext
from homyak.core.watchlist import match

log = structlog.get_logger(__name__)


class WatchlistAnalyzer:
    name = "watchlist"
    stage = 2  # ПО НОМЕРУ до prefilter(3): тот читает watch_topics как whitelist

    async def analyze(self, ctx: AnalyzerContext) -> None:
        # Читаем на каждый айтем, а не в __init__: загрузчик кэширует по mtime, значит правка
        # watch в interests.yaml подхватывается без рестарта — файл смонтирован volume'ом.
        wl = compiled_watchlist()
        if not wl:
            return
        item = ctx.item
        item.watch_topics = match(item.title, item.text, wl)
