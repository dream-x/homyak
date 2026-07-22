"""Backfill LLM-вики из истории ⭐/👍 в БД (таблица feedback).

Сервис homyak-wiki ловит только НОВЫЙ фидбек (и то, что осталось в JetStream). Полная история
⭐/👍 живёт в PG — прогоняем её через тот же ingest_item. Идемпотентно: source-страница
перезаписывается, буллеты по source-ref не задваиваются. Разовый прогон после запуска вики.
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from sqlalchemy import text

from homyak.core import wiki
from homyak.core.llm import OllamaLLM
from homyak.core.wiki_ingest import ingest_item
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

console = Console()


async def main_async() -> None:
    wiki.ensure_dirs()
    repo = NewsRepo(SessionFactory)
    llm = OllamaLLM()
    async with SessionFactory() as s:
        ids = list(
            (
                await s.execute(
                    text(
                        "select distinct news_item_id from feedback"
                        " where signal in ('up','save') order by news_item_id"
                    )
                )
            ).scalars().all()
        )

    console.print(f"[bold]История ⭐/👍: {len(ids)} записей → в вику[/bold]")
    done = skipped = 0
    for item_id in ids:
        item = await repo.get_by_id(item_id)
        if item is None or not (item.title or item.text):
            skipped += 1
            continue
        await ingest_item(item, llm)
        done += 1
        if done % 20 == 0:
            console.print(f"  {done}/{len(ids)} · {wiki.stats()}")

    lint = wiki.run_lint()
    console.print(f"[green]Готово: {done} записей, {skipped} пропущено · {lint['pages']}[/green]")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
