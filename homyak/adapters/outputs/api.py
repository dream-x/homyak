"""FastAPI-приложение: JSON-лента, RSS/JSON feed, детали item'а, admin-хелперы."""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text

from homyak.adapters.outputs import dashboard, json_feed, rss_out, sse
from homyak.core.config import settings
from homyak.core.events import NatsBus
from homyak.core.interfaces import FeedQuery, NewsItemDTO
from homyak.storage.db import SessionFactory, engine
from homyak.storage.postgres import NewsRepo

_bus: NatsBus | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Шина нужна, чтобы 👍/👎 с дашборда обучали learner (publish_feedback). Если NATS недоступен,
    # фидбек всё равно пишется в БД — просто learner не дёрнется в реальном времени.
    global _bus
    _bus = NatsBus(settings.nats_url)
    try:
        await _bus.connect()
    except Exception:
        _bus = None
    yield
    if _bus is not None:
        with suppress(Exception):
            await _bus.close()


app = FastAPI(title="Homyak", version="0.1.0", lifespan=lifespan)
repo = NewsRepo(SessionFactory)


@app.get("/feed/stream")
async def feed_stream(request: Request, category: str | None = None):
    """SSE-поток новых обработанных items (realtime из JetStream)."""
    return await sse.stream_response(request, category)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> str:
    """Realtime-дашборд пайплайна (self-contained HTML)."""
    return dashboard.PAGE


@app.get("/lenta", response_class=HTMLResponse)
async def lenta_page() -> str:
    """Отдельная страница Ленты (фид + 👍/👎 + Разбор), вынесена из операционного дашборда."""
    return dashboard.LENTA_PAGE


@app.get("/search", response_class=HTMLResponse)
async def search_page() -> str:
    """Гибридный поиск по базе знаний (FTS+вектор) + кнопка «Ответить» (RAG/вики)."""
    return dashboard.SEARCH_PAGE


@app.get("/trends", response_class=HTMLResponse)
async def trends_page() -> str:
    """Тренды (день/неделя/месяц): что разгоняется + подборка по теме."""
    return dashboard.TRENDS_PAGE


def _vertical(v: str) -> str | None:
    return v if v in ("business", "it", "medical") else None


@app.get("/trends/api/list")
async def trends_list(period: str = "day", vertical: str = "all") -> dict:
    from homyak.core.trends import PERIODS, compute_trends

    period = period if period in PERIODS else "day"
    return {
        "period": period,
        "vertical": vertical,
        "trends": await compute_trends(period, _vertical(vertical)),
    }


@app.get("/trends/api/items")
async def trends_items(period: str = "day", tag: str = "", vertical: str = "all") -> dict:
    from homyak.core.trends import PERIODS, trend_items

    period = period if period in PERIODS else "day"
    if not tag:
        raise HTTPException(status_code=400, detail="tag required")
    return {
        "period": period,
        "tag": tag,
        "items": await trend_items(tag, period, _vertical(vertical), limit=20),
    }


@app.get("/wiki", response_class=HTMLResponse)
async def wiki_page() -> str:
    """Браузер LLM-вики: концепты/сущности/источники, клик по [[ссылкам]]."""
    return dashboard.WIKI_PAGE


@app.get("/wiki/api/stats")
async def wiki_stats() -> dict:
    from homyak.core import wiki

    return wiki.stats()


@app.get("/wiki/api/list")
async def wiki_list(kind: str = "concepts") -> dict:
    from homyak.core import wiki

    if kind not in wiki.KINDS:
        raise HTTPException(status_code=400, detail="kind must be concepts|entities|sources")
    return {"kind": kind, "pages": wiki.list_with_meta(kind)}


@app.get("/wiki/api/page")
async def wiki_get_page(kind: str, slug: str) -> dict:
    from homyak.core import wiki

    if kind not in wiki.KINDS:
        raise HTTPException(status_code=400, detail="bad kind")
    body = wiki.read_page(kind, slug)
    if body is None:
        raise HTTPException(status_code=404, detail="page not found")
    return {"kind": kind, "slug": slug, "markdown": body}


@app.get("/search/api")
async def search_api(
    q: str,
    kind: str = "all",
    hours: int = 0,
    saved: bool = False,
    limit: int = 40,
    rerank: bool = True,
) -> dict:
    """Гибридный поиск: q → ранжированный список (лексика OR + семантика, RRF + бусты намерений),
    затем LLM-переранжирование под точный смысл (rerank=false — быстрый режим без LLM)."""
    from homyak.core import search as search_mod

    items = await search_mod.hybrid_search(
        q, kind=kind, hours=min(max(hours, 0), 8760), saved=saved, limit=min(max(limit, 1), 100)
    )
    reranked = False
    if rerank and len(items) > 1:
        items = await search_mod.rerank(q, items)
        reranked = True
    return {"q": q, "kind": kind, "hours": hours, "saved": saved, "reranked": reranked, "items": items}


class _AnswerIn(BaseModel):
    q: str


@app.post("/search/answer")
async def search_answer(inp: _AnswerIn) -> dict:
    """Кнопка «Ответить»: выжимка по вопросу. Сперва вики (если есть страницы), иначе RAG по ленте."""
    from homyak.core.ask import answer_question
    from homyak.core.wiki_query import answer_from_wiki

    wiki = await answer_from_wiki(inp.q)
    if wiki and wiki.get("answer"):
        return {**wiki, "source": "wiki"}
    res = await answer_question(inp.q)
    return {**res, "source": "feed"}


@app.get("/dashboard/stream")
async def dashboard_stream(request: Request):
    """SSE-поток событий пайплайна для дашборда (ingested + processed)."""
    return await dashboard.stream_events(request)


@app.get("/dashboard/stats")
async def dashboard_stats() -> dict:
    """Снапшот агрегатов для карточек/панелей дашборда."""
    return await dashboard.stats_snapshot()


@app.get("/dashboard/queue")
async def dashboard_queue(limit: int = 40) -> dict:
    """Очередь на обработку (pending-айтемы)."""
    return await dashboard.queue_snapshot(min(max(limit, 1), 300))


@app.get("/dashboard/item/{item_id}")
async def dashboard_item(item_id: int) -> dict:
    """Карточка айтема (исходное сообщение + мета)."""
    return await dashboard.item_detail(item_id)


@app.get("/dashboard/skipped")
async def dashboard_skipped(limit: int = 60) -> dict:
    """Что отсёк гейт до LLM (с причиной)."""
    return await dashboard.skipped_snapshot(min(max(limit, 1), 300))


@app.get("/dashboard/interests")
async def dashboard_interests() -> dict:
    """Три слоя «что мне нравится»: декларация · выученное · веса (+ дрейф файла и БД)."""
    return await dashboard.interests_snapshot()


@app.get("/dashboard/watchlist")
async def dashboard_watchlist() -> dict:
    """Панели по трендовым темам (счётчик + свежие айтемы на тему)."""
    return await dashboard.watchlist_snapshot()


@app.get("/dashboard/feed")
async def dashboard_feed(kind: str = "all", limit: int = 50, sort: str = "time", hours: int = 0) -> dict:
    """Персистентная лента «что получаем» + фидбек по каждому (kind: all|twitter|верткаль|watch,
    sort: time|score, hours: 0=всё | N=за последние N часов)."""
    sort = sort if sort in ("time", "score") else "time"
    return await dashboard.feed_snapshot(kind, min(max(limit, 1), 200), sort, min(max(hours, 0), 720))


class _FeedbackIn(BaseModel):
    item_id: int
    signal: str  # up | down | save


@app.post("/dashboard/feedback")
async def dashboard_feedback(fb: _FeedbackIn) -> dict:
    """👍/👎/⭐ с дашборда: пишем фидбек и публикуем в NATS → learner обучается (как из бота)."""
    if fb.signal not in ("up", "down", "save"):
        raise HTTPException(status_code=400, detail="signal must be up|down|save")
    action, _ = await repo.record_feedback(fb.item_id, fb.signal, None)
    if _bus is not None:
        with suppress(Exception):
            await _bus.publish_feedback(fb.item_id, fb.signal, None, action=action)
    return {"action": action}


@app.get("/dashboard/razbor/{item_id}")
async def dashboard_razbor(item_id: int) -> dict:
    """📝 Разбор айтема для модалки дашборда (структурированное саммари на русском)."""
    return await dashboard.razbor_snapshot(item_id, repo)


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
        "feed": it.feed_name,
        "tags": it.tags or [],
        "summary": it.summary,
        "score": it.score,
        "personal_score": it.personal_score,
        "llm_reason": it.llm_reason,
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
    sort: str = "recent",
    feed: str | None = None,
) -> FeedQuery:
    return FeedQuery(
        category=category,
        source_types=source_type,
        feed_name=feed,
        since=since,
        limit=min(max(limit, 1), 200),
        cursor=cursor,
        collapse_clusters=collapse,
        sort=sort if sort in ("recent", "score", "personal") else "recent",
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
    sort: str = "recent",
    feed: str | None = None,
) -> dict:
    q = _build_query(category, source_type, since, limit, cursor, collapse, sort, feed)
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
        "summary": row.summary,
        "cluster_id": row.cluster_id,
        "raw_score": row.raw_score,
        "score": row.score,
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
