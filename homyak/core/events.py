"""NATS JetStream: единая внутренняя шина Homyak.

Stream `HOMYAK`, subjects `homyak.items.*`. Публикуют source-адаптеры (`ingested`) и processor
(`processed`); durable pull-consumer'ы делят нагрузку. Единицы `max_age`/`ack_wait` — секунды
(nats-py конвертирует в наносекунды при сериализации).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import nats
import nats.errors
import structlog
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError

log = structlog.get_logger(__name__)

STREAM = "HOMYAK"
SUBJECT_INGESTED = "homyak.items.ingested"
SUBJECT_PROCESSED = "homyak.items.processed"
SUBJECT_OUTPUT = "homyak.items.output"
SUBJECT_FEEDBACK = "homyak.feedback.recorded"
SUBJECT_TELEGRAM_RAW = "homyak.telegram.raw"  # сырые сообщения от tscrapper
SUBJECT_PROFILE_SUGGESTION = "homyak.profile.suggestion"  # предложение правки профиля
_SUBJECTS = [
    "homyak.items.*",
    "homyak.feedback.*",
    "homyak.telegram.*",
    "homyak.profile.*",
]

MAX_AGE_SECONDS = 14 * 24 * 3600  # 14 дней
MAX_BYTES = 5 * 1024**3  # 5 GB


class NatsBus:
    """Тонкая обёртка над JetStream: connect + idempotent stream + publish/consume."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._nc: nats.NATS | None = None
        self._js: JetStreamContext | None = None

    @property
    def js(self) -> JetStreamContext:
        if self._js is None:
            raise RuntimeError("NatsBus не подключён — вызови connect()")
        return self._js

    async def connect(self) -> None:
        self._nc = await nats.connect(
            servers=[self._url],
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnect,
            reconnected_cb=self._on_reconnect,
        )
        self._js = self._nc.jetstream()
        await self._ensure_stream()
        log.info("nats_connected", url=self._url, stream=STREAM)

    async def _on_error(self, e: Exception) -> None:
        log.warning("nats_error", error=str(e))

    async def _on_disconnect(self) -> None:
        log.warning("nats_disconnected")

    async def _on_reconnect(self) -> None:
        log.info("nats_reconnected")

    async def _ensure_stream(self) -> None:
        cfg = StreamConfig(
            name=STREAM,
            subjects=_SUBJECTS,
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
            max_age=MAX_AGE_SECONDS,
            max_bytes=MAX_BYTES,
            num_replicas=1,
        )
        try:
            await self.js.update_stream(config=cfg)
            log.info("nats_stream_updated", stream=STREAM)
        except NotFoundError:
            await self.js.add_stream(config=cfg)
            log.info("nats_stream_created", stream=STREAM)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._js = None

    # --- publish ---

    async def publish_ingested(self, news_item_id: int, source_type: str | None = None) -> None:
        payload = json.dumps(
            {"news_item_id": news_item_id, "source_type": source_type}
        ).encode()
        await self.js.publish(SUBJECT_INGESTED, payload)

    async def publish_processed(
        self, news_item_id: int, cluster_id: int | None, category: str | None = None
    ) -> None:
        payload = json.dumps(
            {"news_item_id": news_item_id, "cluster_id": cluster_id, "category": category}
        ).encode()
        await self.js.publish(SUBJECT_PROCESSED, payload)

    async def publish_telegram_raw(self, payload: dict) -> None:
        await self.js.publish(SUBJECT_TELEGRAM_RAW, json.dumps(payload).encode())

    async def publish_profile_suggestion(self, payload: dict) -> None:
        await self.js.publish(SUBJECT_PROFILE_SUGGESTION, json.dumps(payload).encode())

    async def publish_feedback(
        self,
        news_item_id: int,
        signal: str,
        topic: str | None = None,
        action: str = "added",
    ) -> None:
        payload = json.dumps(
            {
                "news_item_id": news_item_id,
                "signal": signal,
                "topic": topic,
                "action": action,  # added | removed (для отката обучения)
            }
        ).encode()
        await self.js.publish(SUBJECT_FEEDBACK, payload)

    # --- consume ---

    async def consume_ingested(
        self,
        handler: Callable[[dict], Awaitable[None]],
        *,
        durable: str = "processor",
        **kw,
    ) -> None:
        await self._consume(SUBJECT_INGESTED, handler, durable=durable, **kw)

    async def consume_feedback(
        self,
        handler: Callable[[dict], Awaitable[None]],
        *,
        durable: str = "learner",
        **kw,
    ) -> None:
        await self._consume(SUBJECT_FEEDBACK, handler, durable=durable, **kw)

    async def consume_telegram_raw(
        self,
        handler: Callable[[dict], Awaitable[None]],
        *,
        durable: str = "telegram-ingest",
        **kw,
    ) -> None:
        await self._consume(SUBJECT_TELEGRAM_RAW, handler, durable=durable, **kw)

    async def consume_profile_suggestion(
        self,
        handler: Callable[[dict], Awaitable[None]],
        *,
        durable: str = "profile-suggest",
        **kw,
    ) -> None:
        await self._consume(SUBJECT_PROFILE_SUGGESTION, handler, durable=durable, **kw)

    async def _consume(
        self,
        subject: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        durable: str,
        batch: int = 1,
        ack_wait: int = 120,
        max_deliver: int = 5,
        fetch_timeout: float = 5.0,
    ) -> None:
        """Pull-consumer: handler(dict) → успех=ack; исключение=nak+backoff.

        Несколько инстансов с одним `durable` делят нагрузку. После `max_deliver` доставок
        JetStream перестаёт передавать сообщение (dead-letter — Phase 2.5).
        """
        psub = await self.js.pull_subscribe(
            subject,
            durable=durable,
            config=ConsumerConfig(
                ack_wait=ack_wait,
                max_deliver=max_deliver,
                ack_policy=AckPolicy.EXPLICIT,
            ),
        )
        log.info("consumer_started", durable=durable, subject=subject)
        while True:
            try:
                msgs = await psub.fetch(batch=batch, timeout=fetch_timeout)
            except (nats.errors.TimeoutError, asyncio.TimeoutError):
                continue
            except Exception as e:  # переподключение/временный сбой — не роняем loop
                log.warning("fetch_error", error=str(e))
                await asyncio.sleep(1)
                continue

            for msg in msgs:
                try:
                    data = json.loads(msg.data)
                except Exception as e:
                    log.error("bad_message", error=str(e))
                    await msg.term()  # неразбираемое — не реквеуим
                    continue
                try:
                    await handler(data)
                    await msg.ack()
                except Exception as e:
                    delivered = msg.metadata.num_delivered or 1
                    delay = min(2**delivered * 30, 3600)  # exp backoff, cap 1h
                    log.warning(
                        "handler_error",
                        error=str(e),
                        delivered=delivered,
                        nak_delay=delay,
                        **data if isinstance(data, dict) else {},
                    )
                    await msg.nak(delay=delay)
