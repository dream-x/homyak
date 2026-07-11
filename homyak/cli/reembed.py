"""Backfill: переэмбеддить items с устаревшей моделью/версией. Запуск при смене EMBEDDING_MODEL."""

from __future__ import annotations

import asyncio

from rich.console import Console
from sqlalchemy import or_, select

from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import NewsItem
from homyak.storage.db import SessionFactory
from homyak.storage.qdrant import QdrantStore

console = Console()


async def main_async() -> None:
    qdrant = QdrantStore(settings.qdrant_url)
    await qdrant.ensure_collection()
    embedder = EmbedderAnalyzer(qdrant)

    async with SessionFactory() as s:
        ids = list(
            (
                await s.execute(
                    select(NewsItem.id)
                    .where(
                        or_(
                            NewsItem.embedding_version.is_(None),
                            NewsItem.embedding_version < settings.embedding_version,
                            NewsItem.embedding_model != settings.embedding_model,
                        )
                    )
                    .order_by(NewsItem.id)
                )
            ).scalars().all()
        )

    console.print(f"[bold]К переэмбеддингу: {len(ids)}[/bold] (модель={settings.embedding_model} v{settings.embedding_version})")
    done = 0
    for item_id in ids:
        async with SessionFactory() as s:
            item = await s.get(NewsItem, item_id)
            if item is None:
                continue
            ctx = AnalyzerContext(item_id=item_id, item=item, session=s)
            await embedder.analyze(ctx)
            await s.commit()
        done += 1
        if done % 100 == 0:
            console.print(f"  {done}/{len(ids)}")

    console.print(f"[green]Готово: {done} переэмбеддено, точек в Qdrant: {await qdrant.count()}[/green]")
    await qdrant.close()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
