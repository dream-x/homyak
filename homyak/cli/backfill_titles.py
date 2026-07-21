"""Backfill: заголовок обработанным items без него (Telegram/твиты/RSS-огрызки).

Та же генерация, что у стадии title_gen (make_title: LLM + фолбэк-эвристика). Прогон разовый.
По умолчанию трогает только пустые заголовки. С `--regen` заодно ПЕРЕгенерирует заголовки
у источников, которые title вообще не дают (telegram, твиты) — там текущий заголовок всё
равно наш, и его можно улучшить моделью (например после апгрейда эвристики на LLM).
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from sqlalchemy import func, or_, select, update

from homyak.adapters.analyzers.title_gen import make_title
from homyak.core.llm import OllamaLLM
from homyak.core.models import NewsItem
from homyak.storage.db import SessionFactory

console = Console()


async def main_async(regen: bool = False) -> None:
    llm = OllamaLLM()  # та же генерация, что у стадии title_gen
    empty = or_(NewsItem.title.is_(None), func.btrim(NewsItem.title) == "")
    # источники без собственного title: их заголовок всегда сгенерён нами → можно перегенерить
    sourceless = or_(NewsItem.source_type == "telegram", NewsItem.feed_name.like("tw_%"))
    where = or_(empty, sourceless) if regen else empty
    async with SessionFactory() as s:
        rows = list(
            (
                await s.execute(
                    select(NewsItem.id, NewsItem.text, NewsItem.feed_name).where(
                        NewsItem.processed_at.isnot(None), where
                    )
                )
            ).all()
        )
    console.print(f"[dim]режим: {'regen (пустые + telegram/твиты)' if regen else 'только пустые'}[/dim]")

    console.print(f"[bold]Безымянных обработанных: {len(rows)}[/bold]")
    fixed = still_empty = 0
    for item_id, text, feed_name in rows:
        title = await make_title(llm, text, feed_name)
        if not title:
            still_empty += 1
            continue
        async with SessionFactory() as s:
            await s.execute(update(NewsItem).where(NewsItem.id == item_id).values(title=title))
            await s.commit()
        fixed += 1
        if fixed % 50 == 0:
            console.print(f"  {fixed}/{len(rows)}")

    console.print(f"[green]Готово: {fixed} проставлено, {still_empty} без текста и фида (пропущены)[/green]")


def main() -> None:
    asyncio.run(main_async(regen="--regen" in sys.argv))


if __name__ == "__main__":
    main()
