"""Консюмер telegram-ingest: сырые сообщения от tscrapper (NATS homyak.telegram.raw) → PG → ingested.

tscrapper публикует каждое обработанное сообщение в NATS; здесь валидируем, upsert'им (идемпотентно
по source_id) и пушим items.ingested — дальше обычная цепочка (эмбеддинг → судья → personal_score).
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

import structlog

from homyak.adapters.sources.telegram_relay import TelegramOutboxLine
from homyak.core.config import settings
from homyak.core.events import NatsBus
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)

# категории tscrapper → категории Homyak (llm_tagger всё равно перетегирует)
_CAT_MAP = {"news_ai": "ai", "news_tech": "tech"}


def make_handler(repo: NewsRepo, bus: NatsBus):
    async def handle(data: dict) -> None:
        try:
            line = TelegramOutboxLine.model_validate(data)
        except Exception as e:
            log.warning("bad_telegram_raw", error=str(e))
            return  # неразбираемое — не реквеуим (ack)
        dto = line.to_dto()
        dto.category = _CAT_MAP.get(dto.category or "", dto.category)
        item_id, was_new = await repo.upsert_item(dto)
        if was_new:
            await bus.publish_ingested(item_id, "telegram")
            log.info("telegram_ingested", item=item_id, channel=line.author)

    return handle


async def main_async() -> None:
    repo = NewsRepo(SessionFactory)
    bus = NatsBus(settings.nats_url)
    await bus.connect()
    handler = make_handler(repo, bus)
    task = asyncio.create_task(bus.consume_telegram_raw(handler))
    log.info("telegram_ingest_started")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    await stop.wait()

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await bus.close()
    log.info("telegram_ingest_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
