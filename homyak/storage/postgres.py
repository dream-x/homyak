"""Репозиторий доступа к Postgres. Единственное место с SQL для news_items/clusters/ingest_state."""

from __future__ import annotations

import base64

import sqlalchemy as sa
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from homyak.core.interfaces import Feed, FeedQuery, NewsItemDTO
from homyak.core.models import (
    Feedback,
    IngestState,
    NewsItem,
    Profile,
    SourceAffinity,
    TagAffinity,
    TasteState,
)
from homyak.core.scoring import clip
from homyak.core.urls import normalize_url

_POLARITY_WEIGHT = {"love": 0.8, "like": 0.5, "meh": 0.0, "dislike": -0.5, "mute": -1.0}

# поля, обновляемые при повторном приходе того же (source_type, source_id):
_UPSERT_UPDATE = (
    "url",
    "url_normalized",
    "title",
    "text",
    "media",
    "author",
    "feed_name",
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
        feed_name=row.feed_name,
        raw_score=row.raw_score,
        published_at=row.published_at,
        category=row.category,
        tags=list(row.tags or []),
        summary=row.summary,
        score=row.score,
        personal_score=row.personal_score,
        llm_reason=row.llm_reason,
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
            "feed_name": _trunc(item.feed_name, 64),
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
        summary: str | None = None,
        category: str | None = None,
        score: float | None = None,
    ) -> None:
        values: dict = {"processed_at": func.now(), "error": None, "retry_after": None}
        if cluster_id is not None:
            values["cluster_id"] = cluster_id
        if tags is not None:
            values["tags"] = tags
        if summary is not None:
            values["summary"] = summary
        if category is not None:
            values["category"] = category
        if score is not None:
            values["score"] = score
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

    # --- Phase 6: профиль и аффинити ---

    async def get_active_profile(self, vertical: str) -> tuple[int, str, list] | None:
        async with self._sf() as s:
            row = (
                await s.execute(
                    select(Profile.version, Profile.description, Profile.topics).where(
                        Profile.active, Profile.vertical == vertical
                    )
                )
            ).first()
        if row is None:
            return None
        return int(row[0]), row[1], list(row[2] or [])

    async def set_profile(self, vertical: str, description: str, topics: list[dict]) -> int:
        """Новая версия профиля вертикали (+ seed tag_affinity этой вертикали из тем)."""
        async with self._sf() as s:
            cur = await s.scalar(
                select(func.max(Profile.version)).where(Profile.vertical == vertical)
            )
            version = (cur or 0) + 1
            await s.execute(
                update(Profile)
                .where(Profile.active, Profile.vertical == vertical)
                .values(active=False)
            )
            s.add(
                Profile(
                    vertical=vertical,
                    version=version,
                    description=description,
                    topics=topics,
                    active=True,
                )
            )
            for t in topics:
                name = t.get("name")
                if not name:
                    continue
                weight = _POLARITY_WEIGHT.get(t.get("polarity", "like"), 0.0)
                stmt = pg_insert(TagAffinity).values(vertical=vertical, tag=name, weight=weight)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["vertical", "tag"], set_={"weight": weight}
                )
                await s.execute(stmt)
            await s.commit()
        return version

    async def get_tag_affinities(self, vertical: str, tags: list[str]) -> dict[str, float]:
        if not tags:
            return {}
        async with self._sf() as s:
            rows = (
                await s.execute(
                    select(TagAffinity.tag, TagAffinity.weight).where(
                        TagAffinity.vertical == vertical, TagAffinity.tag.in_(list(tags))
                    )
                )
            ).all()
        return {r[0]: float(r[1]) for r in rows}

    async def get_source_affinity(
        self, vertical: str, source_type: str, author: str | None
    ) -> float:
        async with self._sf() as s:
            w = await s.scalar(
                select(SourceAffinity.weight).where(
                    SourceAffinity.vertical == vertical,
                    SourceAffinity.source_type == source_type,
                    SourceAffinity.author == (author or ""),
                )
            )
        return float(w) if w is not None else 0.0

    async def get_taste_n_liked(self, vertical: str) -> int:
        async with self._sf() as s:
            n = await s.scalar(
                select(TasteState.n_liked).where(TasteState.vertical == vertical)
            )
        return int(n) if n is not None else 0

    async def set_taste_state(self, vertical: str, n_liked: int) -> None:
        stmt = pg_insert(TasteState).values(
            vertical=vertical, n_liked=n_liked, updated_at=func.now()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["vertical"], set_={"n_liked": n_liked, "updated_at": func.now()}
        )
        async with self._sf() as s:
            await s.execute(stmt)
            await s.commit()

    # --- Phase 6.1: фидбек и обучение ---

    async def record_feedback(
        self, news_item_id: int, signal: str, topic: str | None = None
    ) -> str:
        """Toggle: первый клик добавляет фидбек ('added'), повторный снимает ('removed')."""
        async with self._sf() as s:
            ins = (
                pg_insert(Feedback)
                .values(news_item_id=news_item_id, signal=signal, topic=topic)
                .on_conflict_do_nothing(index_elements=["news_item_id", "signal"])
                .returning(Feedback.id)
            )
            row = (await s.execute(ins)).first()
            if row is None:
                await s.execute(
                    delete(Feedback).where(
                        Feedback.news_item_id == news_item_id, Feedback.signal == signal
                    )
                )
                await s.commit()
                return "removed"
            await s.commit()
            return "added"

    async def bump_tag_affinity(
        self, vertical: str, tags: list[str], direction: int, lr: float
    ) -> None:
        if not tags:
            return
        async with self._sf() as s:
            for tag in tags:
                row = await s.get(TagAffinity, (vertical, tag))
                if row is None:
                    s.add(
                        TagAffinity(
                            vertical=vertical,
                            tag=tag,
                            weight=clip(lr * direction),
                            n_pos=1 if direction > 0 else 0,
                            n_neg=1 if direction < 0 else 0,
                        )
                    )
                else:
                    row.weight = clip(row.weight + lr * (direction - row.weight))
                    if direction > 0:
                        row.n_pos += 1
                    else:
                        row.n_neg += 1
            await s.commit()

    async def bump_source_affinity(
        self, vertical: str, source_type: str, author: str | None, direction: int, lr: float
    ) -> None:
        key = (vertical, source_type, author or "")
        async with self._sf() as s:
            row = await s.get(SourceAffinity, key)
            if row is None:
                s.add(
                    SourceAffinity(
                        vertical=vertical,
                        source_type=source_type,
                        author=author or "",
                        weight=clip(lr * direction),
                        n_pos=1 if direction > 0 else 0,
                        n_neg=1 if direction < 0 else 0,
                    )
                )
            else:
                row.weight = clip(row.weight + lr * (direction - row.weight))
                if direction > 0:
                    row.n_pos += 1
                else:
                    row.n_neg += 1
            await s.commit()

    async def mute_topic(self, vertical: str, topic: str) -> int:
        """Добавить тему в mute активного профиля вертикали → новая версия профиля."""
        prof = await self.get_active_profile(vertical)
        if prof is None:
            return await self.set_profile(
                vertical,
                f"Интересы (авто). Не интересно: {topic}.",
                [{"name": topic, "polarity": "mute"}],
            )
        _version, desc, topics = prof
        topics = [t for t in topics if t.get("name") != topic]
        topics.append({"name": topic, "polarity": "mute"})
        return await self.set_profile(vertical, desc, topics)

    async def set_item_text(self, news_item_id: int, text: str) -> None:
        async with self._sf() as s:
            await s.execute(
                update(NewsItem).where(NewsItem.id == news_item_id).values(text=text)
            )
            await s.commit()

    async def mark_pushed(self, news_item_id: int) -> None:
        async with self._sf() as s:
            await s.execute(
                update(NewsItem).where(NewsItem.id == news_item_id).values(pushed_at=func.now())
            )
            await s.commit()

    async def count_pushed_since(self, minutes: int) -> int:
        cutoff = func.now() - sa.literal(int(minutes)) * sa.text("interval '1 minute'")
        async with self._sf() as s:
            return (
                await s.scalar(
                    select(func.count())
                    .select_from(NewsItem)
                    .where(NewsItem.pushed_at >= cutoff)
                )
                or 0
            )

    async def digest(self, limit: int = 10) -> list[NewsItem]:
        """Топ непушенных персональных items (для /digest)."""
        async with self._sf() as s:
            rows = (
                await s.execute(
                    select(NewsItem)
                    .where(
                        NewsItem.personal_score.isnot(None),
                        NewsItem.pushed_at.is_(None),
                    )
                    .order_by(NewsItem.personal_score.desc())
                    .limit(limit)
                )
            ).scalars().all()
        return list(rows)

    async def feedback_counts(self) -> dict[str, int]:
        async with self._sf() as s:
            rows = (
                await s.execute(
                    select(Feedback.signal, func.count()).group_by(Feedback.signal)
                )
            ).all()
        return {r[0]: int(r[1]) for r in rows}

    async def learnable_feedback_count(self, vertical: str) -> int:
        async with self._sf() as s:
            n = await s.scalar(
                select(func.count())
                .select_from(Feedback)
                .join(NewsItem, NewsItem.id == Feedback.news_item_id)
                .where(
                    NewsItem.vertical == vertical,
                    Feedback.signal.in_(["up", "down", "save"]),
                )
            )
        return int(n or 0)

    async def recent_liked_disliked(
        self, vertical: str, limit: int = 14
    ) -> tuple[list[tuple[str, list]], list[tuple[str, list]]]:
        """Недавние (title, tags) лайкнутых/дизлайкнутых В ВЕРТИКАЛИ — для рефайнмента профиля."""
        async with self._sf() as s:
            rows = (
                await s.execute(
                    select(Feedback.signal, NewsItem.title, NewsItem.tags)
                    .join(NewsItem, NewsItem.id == Feedback.news_item_id)
                    .where(
                        NewsItem.vertical == vertical,
                        Feedback.signal.in_(["up", "down", "save"]),
                    )
                    .order_by(Feedback.created_at.desc())
                    .limit(limit)
                )
            ).all()
        liked = [(r[1] or "", list(r[2] or [])) for r in rows if r[0] in ("up", "save")]
        disliked = [(r[1] or "", list(r[2] or [])) for r in rows if r[0] == "down"]
        return liked, disliked

    async def get_item_meta(self, news_item_id: int):
        """(source_type, source_key, tags, vertical). source_key = feed_name или author."""
        async with self._sf() as s:
            row = (
                await s.execute(
                    select(
                        NewsItem.source_type,
                        NewsItem.author,
                        NewsItem.feed_name,
                        NewsItem.tags,
                        NewsItem.vertical,
                    ).where(NewsItem.id == news_item_id)
                )
            ).first()
        if row is None:
            return None
        source_key = row[2] or row[1]  # feed_name предпочтительнее author
        return row[0], source_key, list(row[3] or []), row[4]

    async def feed_source_counts(self, limit: int = 30) -> list[tuple[str, int]]:
        """(feed_name, count) обработанных items — для /sources."""
        async with self._sf() as s:
            rows = (
                await s.execute(
                    select(NewsItem.feed_name, func.count())
                    .where(NewsItem.feed_name.isnot(None), NewsItem.processed_at.isnot(None))
                    .group_by(NewsItem.feed_name)
                    .order_by(func.count().desc())
                    .limit(limit)
                )
            ).all()
        return [(r[0], int(r[1])) for r in rows]

    async def feed(self, query: FeedQuery) -> Feed:
        conditions = [NewsItem.processed_at.isnot(None)]
        if query.category:
            conditions.append(NewsItem.category == query.category)
        if query.source_types:
            conditions.append(NewsItem.source_type.in_(query.source_types))
        if query.feed_name:
            conditions.append(NewsItem.feed_name == query.feed_name)
        if query.vertical:
            conditions.append(NewsItem.vertical == query.vertical)
        if query.since:
            conditions.append(NewsItem.published_at >= query.since)
        if query.min_score is not None:
            conditions.append(NewsItem.raw_score >= query.min_score)

        by_score = query.sort == "score"
        by_personal = query.sort == "personal"
        if by_personal:
            conditions.append(NewsItem.personal_score.isnot(None))  # muted/unscored — вон
        sort_ts = _sort_ts()
        if not by_score and not by_personal and query.cursor:  # keyset только для recent
            c_ts, c_id = _decode_cursor(query.cursor)
            conditions.append(
                or_(sort_ts < c_ts, sa.and_(sort_ts == c_ts, NewsItem.id < c_id))
            )

        if by_personal:
            order = [NewsItem.personal_score.desc().nullslast(), NewsItem.id.desc()]
        elif by_score:
            order = [NewsItem.score.desc().nullslast(), NewsItem.id.desc()]
        else:
            order = [sort_ts.desc(), NewsItem.id.desc()]
        # берём окно пошире и коллапсим кластеры в Python (personal-scale — дёшево)
        window = query.limit * 3 if query.collapse_clusters else query.limit + 1
        stmt = select(NewsItem).where(*conditions).order_by(*order).limit(window)
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
        keyset_ok = not by_score and not by_personal
        next_cursor = _encode_cursor(rows[-1]) if has_more and rows and keyset_ok else None
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
