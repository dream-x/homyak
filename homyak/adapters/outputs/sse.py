"""SSE output: реалтайм-события из JetStream items.processed.

Регистрируется напрямую на app в api.py (не через APIRouter — include_router несовместим с
текущей связкой fastapi/starlette). Здесь — только построение потокового ответа.
"""

from __future__ import annotations

import json

import nats
import structlog
from fastapi import Request
from nats.js.api import ConsumerConfig, DeliverPolicy
from sse_starlette.sse import EventSourceResponse

from homyak.core.config import settings
from homyak.core.events import SUBJECT_PROCESSED
from homyak.core.models import NewsItem
from homyak.storage.db import SessionFactory

log = structlog.get_logger(__name__)


async def stream_response(request: Request, category: str | None = None) -> EventSourceResponse:
    async def gen():
        nc = await nats.connect(settings.nats_url)
        js = nc.jetstream()
        sub = await js.subscribe(
            SUBJECT_PROCESSED,
            config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
        )
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await sub.next_msg(timeout=25)
                except Exception:
                    yield {"event": "keepalive", "data": ""}
                    continue

                await msg.ack()
                data = json.loads(msg.data)
                if category and data.get("category") not in (category, None):
                    continue

                async with SessionFactory() as s:
                    item = await s.get(NewsItem, data["news_item_id"])
                if item is None:
                    continue

                payload = {
                    "id": item.id,
                    "source_type": item.source_type,
                    "title": item.title,
                    "url": item.url,
                    "category": item.category,
                    "cluster_id": item.cluster_id,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                }
                yield {"event": "item", "data": json.dumps(payload, ensure_ascii=False)}
        finally:
            await sub.unsubscribe()
            await nc.close()

    return EventSourceResponse(gen())
