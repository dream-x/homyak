"""FastAPI-приложение: JSON-лента, RSS/JSON feed, детали item'а, admin-хелперы."""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import text

from homyak.adapters.outputs import json_feed, rss_out, sse
from homyak.core.interfaces import FeedQuery, NewsItemDTO
from homyak.storage.db import SessionFactory, engine
from homyak.storage.postgres import NewsRepo

app = FastAPI(title="Homyak", version="0.1.0")
repo = NewsRepo(SessionFactory)


@app.get("/feed/stream")
async def feed_stream(request: Request, category: str | None = None):
    """SSE-поток новых обработанных items (realtime из JetStream)."""
    return await sse.stream_response(request, category)


def _item_json(it: NewsItemDTO) -> dict:
    return {
        "id": it.id,
        "source_type": it.source_type,
        "source_id": it.source_id,
        "url": it.url,
        "title": it.title,
        "text": it.text,
        "media": it.media,
        "author": it.author,
        "category": it.category,
        "tags": it.tags or [],
        "cluster_id": it.cluster_id,
        "published_at": it.published_at.isoformat() if it.published_at else None,
    }


def _build_query(
    category: str | None,
    source_type: list[str] | None,
    since: datetime | None,
    limit: int,
    cursor: str | None,
    collapse: bool,
) -> FeedQuery:
    return FeedQuery(
        category=category,
        source_types=source_type,
        since=since,
        limit=min(max(limit, 1), 200),
        cursor=cursor,
        collapse_clusters=collapse,
    )


@app.get("/healthz")
async def healthz() -> dict:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db: {e}") from e
    return {"status": "ok"}


@app.get("/feed")
async def feed(
    category: str | None = None,
    source_type: list[str] | None = Query(default=None),
    since: datetime | None = None,
    limit: int = 100,
    cursor: str | None = None,
    collapse: bool = True,
) -> dict:
    q = _build_query(category, source_type, since, limit, cursor, collapse)
    result = await repo.feed(q)
    return {
        "items": [_item_json(i) for i in result.items],
        "next_cursor": result.next_cursor,
    }


@app.get("/item/{item_id}")
async def item(item_id: int) -> dict:
    row = await repo.get_by_id(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "id": row.id,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "url": row.url,
        "title": row.title,
        "text": row.text,
        "media": list(row.media or []),
        "author": row.author,
        "category": row.category,
        "tags": list(row.tags or []),
        "cluster_id": row.cluster_id,
        "raw_score": row.raw_score,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "processed_at": row.processed_at.isoformat() if row.processed_at else None,
    }


@app.get("/feed.rss")
async def feed_rss(
    category: str | None = None,
    source_type: list[str] | None = Query(default=None),
    limit: int = 100,
) -> Response:
    q = _build_query(category, source_type, None, limit, None, True)
    result = await repo.feed(q)
    return Response(content=rss_out.render(result), media_type="application/rss+xml")


@app.get("/feed.json")
async def feed_json(
    category: str | None = None,
    source_type: list[str] | None = Query(default=None),
    limit: int = 100,
    cursor: str | None = None,
) -> dict:
    q = _build_query(category, source_type, None, limit, cursor, True)
    result = await repo.feed(q)
    return json_feed.render(result)


@app.post("/admin/sources/{name}/repoll")
async def repoll(name: str) -> dict:
    """Dev-хелпер: сбрасывает курсор источника → следующий тик ingest-poll перечитает с нуля."""
    await repo.save_cursor(name, None)
    return {"status": "ok", "source": name, "cursor": None}


@app.post("/admin/clusters/{cluster_id}/split")
async def split_cluster(cluster_id: int) -> dict:
    """Ручной split кластера (защита от false-positive similarity). Полноценно — в Phase 3."""
    raise HTTPException(status_code=501, detail="split реализуется в Phase 3")
