"""Репозиторий доступа к Postgres. Единственное место с SQL для news_items/clusters/ingest_state."""

from __future__ import annotations

import base64

import sqlalchemy as sa
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from homyak.core.interfaces import Feed, FeedQuery, NewsItemDTO
from homyak.core.models import IngestState, NewsItem
from homyak.core.urls import normalize_url

# поля, обновляемые при повторном приходе того же (source_type, source_id):
_UPSERT_UPDATE = (
    "url",
    "url_normalized",
    "title",
    "text",
    "media",
    "author",
    "raw_score",
    "category",
    "published_at",
)


def _sort_ts(col=NewsItem):
    """Единый ключ сортировки ленты: published_at, а если пусто — fetched_at (всегда есть)."""
    return func.coalesce(col.published_at, col.fetched_at)


def _trunc(value: str | None, limit: int) -> str | None:
    """Защита от переполнения varchar-колонок (author у arXiv — десятки авторов и т.п.)."""
    if value is None:
        return None
    return value[:limit]


def _dto(row: NewsItem) -> NewsItemDTO:
    return NewsItemDTO(
        source_type=row.source_type,
        source_id=row.source_id,
        id=row.id,
        url=row.url,
        title=row.title,
        text=row.text,
        media=list(row.media or []),
        author=row.author,
        raw_score=row.raw_score,
        published_at=row.published_at,
        category=row.category,
        tags=list(row.tags or []),
        cluster_id=row.cluster_id,
    )


class NewsRepo:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def upsert_item(self, item: NewsItemDTO) -> tuple[int, bool]:
        """INSERT ... ON CONFLICT (source_type, source_id) DO UPDATE. Возвращает (id, was_new).

        was_new через системную колонку xmax: для вставленной строки xmax=0, для обновлённой — нет.
        """
        values = {
            "source_type": _trunc(item.source_type, 32),
            "source_id": _trunc(item.source_id, 255),
            "url": item.url,
            "url_normalized": normalize_url(item.url),
            "title": item.title,
            "text": item.text,
            "media": item.media or [],
            "author": _trunc(item.author, 255),
            "raw_score": item.raw_score,
            "category": _trunc(item.category, 64),
            "published_at": item.published_at,
        }
        stmt = pg_insert(NewsItem).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_type", "source_id"],
            set_={k: getattr(stmt.excluded, k) for k in _UPSERT_UPDATE},
        ).returning(NewsItem.id, sa.text("(xmax = 0) AS was_new"))

        async with self._sf() as s:
            row = (await s.execute(stmt)).first()
            await s.commit()
        return int(row[0]), bool(row[1])

    async def get_by_id(self, id_: int) -> NewsItem | None:
        async with self._sf() as s:
            return await s.get(NewsItem, id_)

    async def get_cursor(self, source_name: str) -> str | None:
        async with self._sf() as s:
            return await s.scalar(
                select(IngestState.cursor).where(IngestState.source_name == source_name)
            )

    async def save_cursor(
        self, source_name: str, cursor: str | None, error: str | None = None
    ) -> None:
        stmt = pg_insert(IngestState).values(
            source_name=source_name, cursor=cursor, last_run_at=func.now(), last_error=error
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_name"],
            set_={"cursor": stmt.excluded.cursor, "last_run_at": func.now(), "last_error": error},
        )
        async with self._sf() as s:
            await s.execute(stmt)
            await s.commit()

    async def mark_processed(
        self,
        id_: int,
        *,
        cluster_id: int | None = None,
        tags: list[str] | None = None,
        category: str | None = None,
        score: float | None = None,
    ) -> None:
        values: dict = {"processed_at": func.now(), "error": None, "retry_after": None}
        if cluster_id is not None:
            values["cluster_id"] = cluster_id
        if tags is not None:
            values["tags"] = tags
        if category is not None:
            values["category"] = category
        if score is not None:
            values["raw_score"] = score
        async with self._sf() as s:
            await s.execute(update(NewsItem).where(NewsItem.id == id_).values(**values))
            await s.commit()

    async def mark_failed(self, id_: int, error: str) -> None:
        """attempts += 1, exponential retry_after (cap 60 мин)."""
        retry_after = func.now() + func.least(func.power(2, NewsItem.attempts), 60) * sa.text(
            "interval '1 minute'"
        )
        async with self._sf() as s:
            await s.execute(
                update(NewsItem)
                .where(NewsItem.id == id_)
                .values(
                    attempts=NewsItem.attempts + 1,
                    error=error,
                    processing_started_at=None,
                    retry_after=retry_after,
                )
            )
            await s.commit()

    async def unprocessed_stale(self, older_than_minutes: int = 5) -> list[int]:
        """Для sweeper'а: зависшие items, готовые к повторной публикации."""
        cutoff = func.now() - sa.literal(int(older_than_minutes)) * sa.text("interval '1 minute'")
        stmt = (
            select(NewsItem.id)
            .where(
                NewsItem.processed_at.is_(None),
                NewsItem.fetched_at < cutoff,
                NewsItem.attempts < 5,
                or_(NewsItem.retry_after.is_(None), NewsItem.retry_after < func.now()),
            )
            .order_by(NewsItem.id)
            .limit(500)
        )
        async with self._sf() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def feed(self, query: FeedQuery) -> Feed:
        conditions = [NewsItem.processed_at.isnot(None)]
        if query.category:
            conditions.append(NewsItem.category == query.category)
        if query.source_types:
            conditions.append(NewsItem.source_type.in_(query.source_types))
        if query.since:
            conditions.append(NewsItem.published_at >= query.since)
        if query.min_score is not None:
            conditions.append(NewsItem.raw_score >= query.min_score)

        sort_ts = _sort_ts()
        if query.cursor:
            c_ts, c_id = _decode_cursor(query.cursor)
            conditions.append(
                or_(sort_ts < c_ts, sa.and_(sort_ts == c_ts, NewsItem.id < c_id))
            )

        # берём окно пошире и коллапсим кластеры в Python (personal-scale — дёшево)
        window = query.limit * 3 if query.collapse_clusters else query.limit + 1
        stmt = (
            select(NewsItem)
            .where(*conditions)
            .order_by(sort_ts.desc(), NewsItem.id.desc())
            .limit(window)
        )
        async with self._sf() as s:
            rows = list((await s.execute(stmt)).scalars().all())

        if query.collapse_clusters:
            seen: set[int] = set()
            picked: list[NewsItem] = []
            for r in rows:
                key = r.cluster_id or r.id
                if key in seen:
                    continue
                seen.add(key)
                picked.append(r)
                if len(picked) > query.limit:
                    break
            rows = picked

        has_more = len(rows) > query.limit
        rows = rows[: query.limit]
        next_cursor = _encode_cursor(rows[-1]) if has_more and rows else None
        return Feed(items=[_dto(r) for r in rows], next_cursor=next_cursor)


def _encode_cursor(row: NewsItem) -> str:
    ts = row.published_at or row.fetched_at
    raw = f"{ts.isoformat()}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str):
    from datetime import datetime

    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = raw.rsplit("|", 1)
    return datetime.fromisoformat(ts_str), int(id_str)
