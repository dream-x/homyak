"""Analyzer stage 2: матч айтема на трендовые темы вотчлиста (config/watchlist.yaml).

Без LLM — ключи/синонимы (RU+EN). Ставит ctx.item.watch_topics (персистит processor).
Читает уже дозагруженный article_fetch'ем текст (stage 0), поэтому матч по полному тексту.
"""

from __future__ import annotations

import structlog

from homyak.core.interfaces import AnalyzerContext
from homyak.core.watchlist import load_watchlist, match

log = structlog.get_logger(__name__)


class WatchlistAnalyzer:
    name = "watchlist"
    stage = 2  # ПО НОМЕРУ до prefilter(3): тот читает watch_topics как whitelist

    def __init__(self) -> None:
        self._wl = load_watchlist()

    async def analyze(self, ctx: AnalyzerContext) -> None:
        if not self._wl:
            return
        item = ctx.item
        item.watch_topics = match(item.title, item.text, self._wl)
