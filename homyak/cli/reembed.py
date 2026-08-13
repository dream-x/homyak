"""Разом переэмбеддить всё устаревшее — ручной дублёр планировщика в sweeper'е.

Штатно очередь разбирается сама (см. `core/reembed`, задание `reembed` в sweeper'е). Этот
вход нужен, когда ждать порций не хочется: сменили EMBEDDING_MODEL, прогнали большой бэкфилл
текстов, подняли систему после долгого простоя.

    homyak-reembed [--limit=N]
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console

from homyak.core.config import settings
from homyak.core.reembed import reembed
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo
from homyak.storage.qdrant import QdrantStore

console = Console()


async def main_async() -> None:
    limit = next(
        (int(a.split("=", 1)[1]) for a in sys.argv[1:] if a.startswith("--limit=")), None
    )
    ids = await NewsRepo(SessionFactory).stale_embedding_ids(limit)
    console.print(
        f"[bold]К переэмбеддингу: {len(ids)}[/bold]"
        f" (модель={settings.embedding_model} v{settings.embedding_version})"
    )
    if not ids:
        return

    qdrant = QdrantStore(settings.qdrant_url)
    try:
        done = await reembed(ids, qdrant)
        console.print(
            f"[green]Готово: {done} переэмбеддено, точек в Qdrant: {await qdrant.count()}[/green]"
        )
    finally:
        await qdrant.close()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
