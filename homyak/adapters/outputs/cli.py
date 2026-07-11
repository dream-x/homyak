"""CLI-просмотрщик ленты: топ новостей по score в терминале (rich). Токен не нужен."""

from __future__ import annotations

import asyncio
import os

from rich.console import Console
from rich.table import Table

from homyak.core.interfaces import FeedQuery
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo


async def main_async(limit: int = 30) -> None:
    repo = NewsRepo(SessionFactory)
    feed = await repo.feed(FeedQuery(sort="score", limit=limit))

    table = Table(title="Homyak — топ ленты по score")
    table.add_column("score", justify="right", style="cyan")
    table.add_column("src", style="magenta")
    table.add_column("заголовок")
    table.add_column("теги", style="green")

    for it in feed.items:
        table.add_row(
            f"{it.score:.3f}" if it.score is not None else "—",
            it.source_type,
            (it.title or "")[:70],
            ", ".join((it.tags or [])[:3]),
        )
    Console().print(table)


def main() -> None:
    limit = int(os.getenv("HOMYAK_CLI_LIMIT", "30"))
    asyncio.run(main_async(limit))


if __name__ == "__main__":
    main()
