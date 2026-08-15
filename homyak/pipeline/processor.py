"""JetStream durable consumer `processor`: гоняет analyzer-pipeline над каждым ingested item'ом."""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

import structlog
from nats.js.api import DeliverPolicy

from homyak.core.config import settings
from homyak.core.events import NatsBus
from homyak.core.interfaces import AnalyzerContext
from homyak.core.models import NewsItem
from homyak.core.registry import build_analyzers
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo
from homyak.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)


def make_handler(repo: NewsRepo, bus: NatsBus, analyzers):
    async def handle(data: dict) -> None:
        item_id = data["news_item_id"]
        try:
            async with SessionFactory() as s:
                item = await s.get(NewsItem, item_id)
                if item is None:
                    log.warning("item_gone", news_item_id=item_id)
                    return  # ack — обрабатывать нечего
                if item.processed_at is not None:
                    return  # идемпотентный ack — уже обработан

                ctx = AnalyzerContext(item_id=item_id, item=item, session=s)
                try:
                    for analyzer in analyzers:
                        await analyzer.analyze(ctx)
                        if ctx.skip:  # гейт отсеял — дальше (LLM) не идём
                            item.skip_reason = ctx.skip_reason
                            break
                    await s.commit()  # persist новые кластеры + мутации item'а
                except Exception:
                    await s.rollback()
                    raise

            if ctx.skip:
                # Помечаем обработанным без score → в ленты/пуши не попадёт, sweeper не воскресит.
                await repo.mark_processed(item_id, cluster_id=ctx.cluster_id)
                log.info("prefiltered", news_item_id=item_id, reason=ctx.skip_reason)
                return

            await repo.mark_processed(
                item_id,
                cluster_id=ctx.cluster_id,
                tags=ctx.tags,
                summary=ctx.summary,
                score=ctx.score,
            )
            await bus.publish_processed(item_id, ctx.cluster_id, ctx.item.category)
            log.info("processed", news_item_id=item_id, cluster_id=ctx.cluster_id)
        except Exception as e:
            await repo.mark_failed(item_id, str(e))
            raise  # → bus.nak с backoff

    return handle


async def main_async() -> None:
    repo = NewsRepo(SessionFactory)
    bus = NatsBus(settings.nats_url)
    await bus.connect()
    qdrant = QdrantStore(settings.qdrant_url)
    await qdrant.ensure_collection()
    await qdrant.ensure_taste_collection()
    analyzers = build_analyzers(qdrant, repo)
    log.info("processor_starting", analyzers=[a.name for a in analyzers])

    handler = make_handler(repo, bus, analyzers)
    # DeliverPolicy.NEW действует только при СОЗДАНИИ durable; существующий продолжает со своей
    # позиции, поэтому простой процессора ничего не теряет. Нужно это для восстановления: когда
    # консюмер захлебнулся (у нас накопилось 266 000 сообщений на 4872 записи), его удаляют — и
    # пересозданный обязан начать с конца потока, а не проигрывать полмиллиона сообщений заново.
    # Непрочёсанное не теряется: оно лежит в Postgres с processed_at IS NULL, и sweeper вернёт его.
    consume_task = asyncio.create_task(
        bus.consume_ingested(handler, durable="processor", deliver_policy=DeliverPolicy.NEW)
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    consume_task.cancel()
    with suppress(asyncio.CancelledError):
        await consume_task
    await bus.close()
    await qdrant.close()
    log.info("processor_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
