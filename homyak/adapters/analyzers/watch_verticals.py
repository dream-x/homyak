"""Analyzer stage 4 (сразу после llm_tagger): снимает темы вотчлиста, ограниченные вертикалями
(`verticals:` в config/interests.yaml), с записей других вертикалей.

Матчер (стадия 2) вертикали не знает — её ставит теггер; здесь она уже есть. Неизвестная
вертикаль (other) метку не снимает. Белый список префильтра (стадия 3) такая тема по-прежнему
даёт всем вертикалям — ограничение про метку в пульсе/таймлайне, не про гейт.
"""

from __future__ import annotations

import structlog

from homyak.core.interests import compiled_watchlist
from homyak.core.interfaces import AnalyzerContext
from homyak.core.watchlist import restrict_to_verticals

log = structlog.get_logger(__name__)


class WatchVerticalsAnalyzer:
    name = "watch_verticals"
    stage = 4  # ПО ПОРЯДКУ после llm_tagger(4): registry добавляет следом, сортировка стабильна

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        before = list(item.watch_topics or [])
        if not before:
            return
        kept = restrict_to_verticals(before, item.vertical, compiled_watchlist())
        if kept != before:
            log.info(
                "watch_topics_restricted",
                news_item_id=ctx.item_id,
                vertical=item.vertical,
                dropped=sorted(set(before) - set(kept)),
            )
            item.watch_topics = kept
