"""Entrypoint telegram-relay: тейлит outbox → upsert в PG → publish ingested."""

from __future__ import annotations

import asyncio
import signal

import structlog

from homyak.adapters.sources.telegram_relay import TelegramRelaySource
from homyak.core.config import settings
from homyak.core.events import NatsBus
from homyak.core.interfaces import NewsItemDTO
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)


async def main_async() -> None:
    repo = NewsRepo(SessionFactory)
    bus = NatsBus(settings.nats_url)
    await bus.connect()
    source = TelegramRelaySource(settings.telegram_outbox_path, repo)

    async def sink(dto: NewsItemDTO) -> None:
        item_id, was_new = await repo.upsert_item(dto)
        if was_new:
            await bus.publish_ingested(item_id, dto.source_type)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, source.stop)

    log.info("tg_relay_started", outbox=settings.telegram_outbox_path)
    await source.subscribe(sink)
    await bus.close()
    log.info("tg_relay_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
