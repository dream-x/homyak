"""Backfill: проставить заголовок обработанным items без него (Telegram/твиты/RSS-огрызки).

Та же чистая логика, что у стадии title_gen (core.titles.derive_title) — прогоняем её по
уже накопленным безымянным items. Идемпотентно: с заголовком не трогаем. Запуск после
деплоя title_gen, разово.
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from sqlalchemy import func, or_, select, update

from homyak.core.models import NewsItem
from homyak.core.titles import derive_title
from homyak.storage.db import SessionFactory

console = Console()


async def main_async() -> None:
    async with SessionFactory() as s:
        rows = list(
            (
                await s.execute(
                    select(NewsItem.id, NewsItem.text, NewsItem.feed_name).where(
                        NewsItem.processed_at.isnot(None),
                        or_(NewsItem.title.is_(None), func.btrim(NewsItem.title) == ""),
                    )
                )
            ).all()
        )

    console.print(f"[bold]Безымянных обработанных: {len(rows)}[/bold]")
    fixed = still_empty = 0
    for item_id, text, feed_name in rows:
        fallback = f"Запись {feed_name}" if feed_name else None
        title = derive_title(text, fallback=fallback)
        if not title:
            still_empty += 1
            continue
        async with SessionFactory() as s:
            await s.execute(update(NewsItem).where(NewsItem.id == item_id).values(title=title))
            await s.commit()
        fixed += 1
        if fixed % 100 == 0:
            console.print(f"  {fixed}/{len(rows)}")

    console.print(f"[green]Готово: {fixed} проставлено, {still_empty} без текста и фида (пропущены)[/green]")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
