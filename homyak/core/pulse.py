"""Пульс вертикали за окно (по умолчанию бизнес, 24 ч): что обсуждают все, кто из вотчлиста
всплыл, какие темы разгоняются. Без LLM — только структура данных.

- «Обсуждают все»: история (кластер) ранжируется по ШИРИНЕ — числу независимых лент, которые её
  принесли, затем по числу записей и по personal_score. Одна лента с десятью постами — не событие;
  пять лент с одним постом каждая — событие.
- Вотчлист: сколько историй упоминали тему в окне против типичного окна (среднее по 7 предыдущим).
- Темы: верх дневных трендов той же вертикали (core.trends).
Чистые части (ранжирование, дельта вотчлиста) тестируются без БД.
"""

from __future__ import annotations

from sqlalchemy import text

from homyak.core.interests import watch_topic_names
from homyak.core.trends import direction, growth, trend_report
from homyak.storage import db

_VERTICALS = ("business", "it", "medical")
MAX_HOURS = 24 * 14


def breadth_rank(stories: list[dict], limit: int) -> list[dict]:
    """Шире → громче → релевантнее. `wide` — три и больше независимых лент."""
    out = sorted(
        stories,
        key=lambda s: (s["feeds"], s["items"], s.get("score") or 0.0),
        reverse=True,
    )
    for s in out:
        s["wide"] = s["feeds"] >= 3
    return out[:limit]


def watch_delta(name: str, count: int, base: float) -> dict:
    """Тема вотчлиста: сколько историй в окне против типичного окна."""
    g = growth(count, base)
    return {
        "name": name,
        "count": int(count),
        "base": round(float(base), 1),
        "growth": round(max(min(g, 9.99), -1.0), 2),
        "direction": direction(count, base, g),
    }


async def pulse(
    hours: int = 24, vertical: str | None = "business", limit: int = 10, sf=None
) -> dict:
    """Пульс за `hours` часов: {stories, watch, trends}."""
    hours = max(1, min(int(hours or 24), MAX_HOURS))
    vcond = " and vertical = :v" if vertical in _VERTICALS else ""
    params: dict = {"h": hours}
    if vcond:
        params["v"] = vertical
    base_filter = (
        " from news_items where processed_at is not null and skip_reason is null" + vcond
    )
    async with (sf or db.SessionFactory)() as s:
        rows = (
            await s.execute(
                text(
                    "with w as (select coalesce(cluster_id, -id) story, id, title, url, feed_name,"
                    "   vertical, personal_score, summary, tags,"
                    "   coalesce(published_at, fetched_at) published,"
                    "   extract(epoch from (now() - coalesce(published_at, fetched_at)))::int age_s"
                    + base_filter +
                    "   and coalesce(published_at, fetched_at) > now() - make_interval(hours => :h)),"
                    " agg as (select story, count(distinct coalesce(feed_name, '')) feeds,"
                    "   count(*) items, max(personal_score) score from w group by story),"
                    " best as (select distinct on (story) * from w"
                    "   order by story, personal_score desc nulls last, published desc)"
                    " select a.story, a.feeds, a.items, a.score, b.id, b.title, b.url, b.feed_name,"
                    "   b.vertical, b.summary, b.tags, b.published, b.age_s"
                    " from agg a join best b using (story)"
                    " order by a.feeds desc, a.items desc, a.score desc nulls last limit :lim"
                ),
                {**params, "lim": limit * 2},
            )
        ).all()
        watch_rows = (
            await s.execute(
                text(
                    "select topic,"
                    "  count(distinct coalesce(cluster_id, -id)) filter (where recent) cur,"
                    "  count(distinct coalesce(cluster_id, -id)) filter (where not recent) / 7.0 base"
                    " from (select cluster_id, id, watch_topics,"
                    "   coalesce(published_at, fetched_at) > now() - make_interval(hours => :h) recent"
                    + base_filter +
                    "   and coalesce(published_at, fetched_at) > now() - make_interval(hours => :h * 8)"
                    "   and cardinality(watch_topics) > 0) x, unnest(watch_topics) topic"
                    " group by topic"
                ),
                params,
            )
        ).all()

    stories = breadth_rank(
        [
            {
                "id": r.id,
                "title": r.title,
                "url": r.url,
                "feed": r.feed_name,
                "vertical": r.vertical,
                "score": r.score,
                "feeds": int(r.feeds),
                "items": int(r.items),
                "summary": (r.summary or "")[:240] or None,
                "tags": list(r.tags or [])[:4],
                "published": r.published,
                "age_s": r.age_s,
            }
            for r in rows
        ],
        limit,
    )
    known = set(watch_topic_names())
    watch = [
        watch_delta(r.topic, int(r.cur), float(r.base or 0))
        for r in watch_rows
        if r.topic in known and (int(r.cur) > 0 or float(r.base or 0) >= 1)
    ]
    watch.sort(key=lambda w: (w["count"], w["growth"]), reverse=True)
    trends, _ = await trend_report("day", vertical if vcond else None, limit=6, sf=sf)
    return {
        "hours": hours,
        "vertical": vertical if vcond else "all",
        "stories": stories,
        "watch": watch,
        "trends": trends,
    }
