"""Таймлайн темы: сколько историй о ней было в каждом окне, где пики, что было главным.

Тема задаётся как угодно: имя из watch (`config/interests.yaml`) → точное совпадение по
`watch_topics`; тег → канон (core.tags, все исторические написания); просто текст → FTS по
`search_tsv` (AND всех слов, префиксы для словоформ: «иран» ловит «иранский»). Три пути
объединяются OR — всё, что «про тему», в одну историю (кластер) считается один раз.

Чистые части (единица времени, пики, спарклайн, tsquery, разбор режима) тестируются без БД;
один SQL (`_rows`) отдаёт окна с нулями, счёт историй и лучшую запись окна.
"""

from __future__ import annotations

import re
from datetime import datetime
from statistics import median

from sqlalchemy import text

from homyak.core.interests import load_interests
from homyak.core.tags import normalize_tag, spelling_map, spellings_of
from homyak.storage import db

_WORD = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ]{2,}")
_BARS = "▁▂▃▄▅▆▇█"
UNITS = {"day": "1 day", "week": "1 week", "month": "1 month"}  # literal → SQL interval
MAX_DAYS = 400


def pick_unit(days: int) -> str:
    """≤ 5 недель — по дням, ≤ 200 дней — по неделям, дальше — по месяцам (10–30 точек)."""
    if days <= 35:
        return "day"
    if days <= 200:
        return "week"
    return "month"


def tsquery_for(query: str) -> str | None:
    """AND всех значимых слов; слова от 4 букв — с префиксом (словоформы). Только alnum-токены,
    поэтому to_tsquery безопасен без экранирования."""
    toks: list[str] = []
    for w in _WORD.findall(query or ""):
        w = w.lower()
        if w not in toks:
            toks.append(w)
    if not toks:
        return None
    return " & ".join(f"{w}:*" if len(w) >= 4 else w for w in toks)


def resolve_watch(query: str, topics=None) -> str | None:
    """Имя темы вотчлиста, если запрос — её имя или алиас (без учёта регистра)."""
    q = (query or "").strip().lower()
    if not q:
        return None
    for t in topics if topics is not None else load_interests().watch:
        if q == t.name.lower() or q in {a.lower() for a in t.aliases}:
            return t.name
    return None


def peaks(counts: list[int]) -> list[bool]:
    """Пик — окно, где историй минимум втрое больше медианы ненулевых окон (и не меньше 3)."""
    nonzero = [c for c in counts if c > 0]
    if len(nonzero) < 2:
        return [False] * len(counts)
    bar = max(3.0, 3.0 * median(nonzero))
    return [c >= bar for c in counts]


def sparkline(counts: list[int]) -> str:
    top = max(counts) if counts else 0
    if top <= 0:
        return _BARS[0] * len(counts)
    return "".join(_BARS[min(7, round(c / top * 7))] for c in counts)


def _sql(window: str, conds: list[str], unit: str) -> str:
    return (
        "with hits as (select coalesce(cluster_id, -id) story, id, title, url, feed_name, vertical,"
        "   personal_score, coalesce(published_at, fetched_at) at,"
        "   date_trunc(:unit, coalesce(published_at, fetched_at)) bucket"
        "   from news_items where " + window +
        f"   and ({' or '.join(conds)})),"
        " per_story as (select distinct on (story) story, bucket, id, title, url, feed_name,"
        "   vertical, personal_score from hits"
        "   order by story, personal_score desc nulls last, at desc),"
        " grid as (select generate_series(date_trunc(:unit, now() - make_interval(days => :days)),"
        f"   date_trunc(:unit, now()), interval '{UNITS[unit]}') bucket),"  # literal: свой словарь
        " agg as (select bucket, count(*) n from per_story group by bucket),"
        " top as (select distinct on (bucket) bucket, id, title, url, feed_name, vertical,"
        "   personal_score from per_story order by bucket, personal_score desc nulls last)"
        " select g.bucket, coalesce(a.n, 0) n, t.id, t.title, t.url, t.feed_name, t.vertical,"
        "   t.personal_score from grid g left join agg a using (bucket) left join top t using (bucket)"
        " order by g.bucket"
    )


async def timeline(
    query: str, days: int = 90, vertical: str | None = None, sf=None
) -> dict:
    """Таймлайн темы за `days` дней: окна (с нулями), счёт историй, лучшая запись окна, пики."""
    q = (query or "").strip()
    days = max(1, min(int(days or 90), MAX_DAYS))
    unit = pick_unit(days)
    watch = resolve_watch(q)
    # тег — канон запроса и канон имени вотчлиста («иран» → Iran → iran): алиасы на другом языке
    # иначе не видят записи с тегом
    tags = [c for c in {normalize_tag(x) for x in (q, watch) if x and len(x) <= 40} if c]
    tsq = tsquery_for(q)
    modes = [m for m, on in (("watch", watch), ("tag", tags), ("text", tsq)) if on]
    if not modes:
        return {"query": q, "modes": [], "unit": unit, "days": days, "total": 0, "buckets": []}

    conds, params = [], {"days": days, "unit": unit}
    vcond = ""
    if vertical in ("business", "it", "medical"):
        vcond = " and vertical = :v"
        params["v"] = vertical
    window = (
        "processed_at is not null and skip_reason is null" + vcond
        + " and coalesce(published_at, fetched_at) > now() - make_interval(days => :days)"
    )
    if watch:
        conds.append("watch_topics @> array[cast(:watch as text)]")
        params["watch"] = watch
    if tsq:
        conds.append("search_tsv @@ to_tsquery('simple', :tsq)")
        params["tsq"] = tsq

    async with (sf or db.SessionFactory)() as s:
        if tags:
            # написания темы в окне (Python-канон по различным тегам) → GIN по tags; все три
            # условия индексируемы, планировщик складывает их в BitmapOr
            raws, canons = await spelling_map(s, window, params)
            spellings = spellings_of(raws, canons, tags)
            if spellings:
                conds.append("tags && cast(:spellings as text[])")
                params["spellings"] = spellings
            else:
                modes.remove("tag")
        if not conds:
            return {"query": q, "modes": [], "unit": unit, "days": days, "total": 0, "buckets": []}
        rows = (await s.execute(text(_sql(window, conds, unit)), params)).all()
    counts = [int(r.n) for r in rows]
    flags = peaks(counts)
    buckets = []
    for r, is_peak in zip(rows, flags):
        start: datetime = r.bucket
        buckets.append(
            {
                "start": start.isoformat(),
                "count": int(r.n),
                "peak": is_peak,
                "top": (
                    {
                        "id": r.id, "title": r.title, "url": r.url, "feed": r.feed_name,
                        "vertical": r.vertical, "score": r.personal_score,
                    }
                    if r.id is not None
                    else None
                ),
            }
        )
    return {
        "query": q,
        "modes": modes,
        "watch": watch,
        "tags": tags,
        "unit": unit,
        "days": days,
        "total": sum(counts),
        "spark": sparkline(counts),
        "buckets": buckets,
    }
