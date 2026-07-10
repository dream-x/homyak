"""Fallback sweep: раз в 5 минут переопубликовывает зависшие items (защита от event loss)."""

from __future__ import annotations

import asyncio
import signal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from homyak.core.config import settings
from homyak.core.events import NatsBus
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)


async def sweep(repo: NewsRepo, bus: NatsBus) -> None:
    ids = await repo.unprocessed_stale(older_than_minutes=5)
    for item_id in ids:
        await bus.publish_ingested(item_id)
    if ids:
        log.info("swept", republished=len(ids))


async def main_async() -> None:
    repo = NewsRepo(SessionFactory)
    bus = NatsBus(settings.nats_url)
    await bus.connect()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sweep, CronTrigger(minute="*/5"), args=[repo, bus], id="sweep", max_instances=1
    )
    scheduler.start()
    log.info("sweeper_started")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    scheduler.shutdown(wait=True)
    await bus.close()
    log.info("sweeper_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
