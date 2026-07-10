"""APScheduler-runner для PollSource'ов: опрашивает источники, upsert'ит, публикует ingested."""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from homyak.core.config import load_sources_config, settings
from homyak.core.events import NatsBus
from homyak.core.interfaces import PollSource
from homyak.core.registry import build_poll_sources
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)


async def run_source(source: PollSource, repo: NewsRepo, bus: NatsBus) -> None:
    cursor = await repo.get_cursor(source.name)
    final = cursor
    new_count = 0
    try:
        async for item, new_cursor in source.poll(cursor):
            item_id, was_new = await repo.upsert_item(item)
            if was_new:
                await bus.publish_ingested(item_id, item.source_type)
                new_count += 1
            final = new_cursor or final
        await repo.save_cursor(source.name, final)
        if new_count:
            log.info("ingested", source=source.name, new=new_count)
    except Exception as e:  # один сбойный источник не должен ронять остальные
        log.warning("source_run_failed", source=source.name, error=str(e))
        await repo.save_cursor(source.name, final, error=str(e))


async def main_async() -> None:
    cfg = load_sources_config()
    sources = build_poll_sources(cfg)
    repo = NewsRepo(SessionFactory)
    bus = NatsBus(settings.nats_url)
    await bus.connect()

    scheduler = AsyncIOScheduler()
    for src in sources:
        scheduler.add_job(
            run_source,
            IntervalTrigger(seconds=src.interval_seconds),
            args=[src, repo, bus],
            id=src.name,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(),  # первый прогон сразу
        )
    scheduler.start()
    log.info("ingest_poll_started", sources=[s.name for s in sources])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    scheduler.shutdown(wait=True)
    await bus.close()
    log.info("ingest_poll_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
