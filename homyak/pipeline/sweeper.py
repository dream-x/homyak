"""Фоновое обслуживание: пересев зависших items и разбор очереди на переэмбеддинг.

Сюда же, а не отдельным сервисом: обе работы периодические, редкие и без внешнего эффекта —
своего процесса не стоят, а APScheduler тут уже поднят.
"""

from __future__ import annotations

import asyncio
import signal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from homyak.core.config import settings
from homyak.core.events import NatsBus
from homyak.core.reembed import reembed
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)


async def sweep(repo: NewsRepo, bus: NatsBus) -> None:
    ids = await repo.unprocessed_stale(older_than_minutes=5)
    for item_id in ids:
        await bus.publish_ingested(item_id)
    if ids:
        log.info("swept", republished=len(ids))


async def reembed_stale(repo: NewsRepo) -> None:
    """Разбор очереди на переэмбеддинг — порциями, чтобы разовый бэкфилл не занял час.

    Очередь наполняется сама: переписал текст записи (дотянули статью, подтянули README) —
    её вектор объявлен устаревшим. Раньше это чинилось ручным запуском `homyak-reembed`,
    то есть не чинилось: бэкфилл README переписал 883 записи, и поиск ещё сутки искал их
    по прежним, коротким текстам.
    """
    ids = await repo.stale_embedding_ids(limit=settings.reembed_batch)
    if not ids:
        return
    done = await reembed(ids)
    log.info("reembedded", done=done, left=await repo.stale_embedding_count())


async def main_async() -> None:
    repo = NewsRepo(SessionFactory)
    bus = NatsBus(settings.nats_url)
    await bus.connect()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sweep, CronTrigger(minute="*/5"), args=[repo, bus], id="sweep", max_instances=1
    )
    if settings.reembed_every_minutes > 0:
        scheduler.add_job(
            reembed_stale,
            CronTrigger(minute=f"*/{settings.reembed_every_minutes}"),
            args=[repo],
            id="reembed",
            max_instances=1,  # долгий прогон не должен наслаиваться сам на себя
        )
    scheduler.start()
    log.info(
        "sweeper_started",
        reembed_every_min=settings.reembed_every_minutes,
        reembed_batch=settings.reembed_batch,
    )

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
