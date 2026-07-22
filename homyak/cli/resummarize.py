"""Backfill: перегенерировать саммари, у которых язык разъехался с текстом.

Точечно чинит старые записи (русская статья → английское саммари от прежнего промпта).
Отбираем в SQL кириллический текст + латинское саммари, подтверждаем detect_lang в Python,
прогоняем llm_summarizer заново и переписываем summary. Разовый прогон после фикса промпта.
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from sqlalchemy import select, text, update

from homyak.adapters.analyzers.llm_summarizer import LlmSummarizerAnalyzer
from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import NewsItem
from homyak.core.textutils import detect_lang
from homyak.storage.db import SessionFactory

console = Console()

# грубый предфильтр в SQL: много кириллицы в тексте, почти нет в саммари → кандидат на почин
_CANDIDATES = text(
    "select id from news_items where summary is not null and btrim(summary) <> ''"
    " and length(regexp_replace(text, '[^а-яёА-ЯЁ]', '', 'g')) > 40"
    " and length(regexp_replace(summary, '[^а-яёА-ЯЁ]', '', 'g')) < 5"
    " order by id"
)


async def main_async() -> None:
    analyzer = LlmSummarizerAnalyzer()
    async with SessionFactory() as s:
        ids = list((await s.execute(_CANDIDATES)).scalars().all())

    console.print(f"[bold]Кандидатов (кириллич. текст + латинское саммари): {len(ids)}[/bold]")
    fixed = skipped = 0
    for item_id in ids:
        async with SessionFactory() as s:
            item = await s.get(NewsItem, item_id)
            if item is None:
                continue
            # подтверждаем несовпадение языка (SQL-фильтр грубоват)
            if not (detect_lang(item.text) == "ru" and detect_lang(item.summary) == "en"):
                skipped += 1
                continue
            ctx = AnalyzerContext(item_id=item_id, item=item, session=s)
            await analyzer.analyze(ctx)
            if ctx.summary and detect_lang(ctx.summary) == "ru":
                await s.execute(
                    update(NewsItem).where(NewsItem.id == item_id).values(summary=ctx.summary)
                )
                await s.commit()
                fixed += 1
            else:
                skipped += 1
        if (fixed + skipped) % 25 == 0:
            console.print(f"  {fixed + skipped}/{len(ids)} (починено {fixed})")

    console.print(f"[green]Готово: {fixed} перегенерировано, {skipped} пропущено[/green]")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
