"""Тренды: какие темы (теги) «разгоняются» за период. Формирование тренда — здесь.

Тренд = тег с высокой силой в окне: сила = объём (сколько историй) × рост (насколько больше, чем
в прошлом таком же окне) × релевантность (средний personal_score — что тебе заходит, наверху).
Периоды: день (vs вчера), неделя (vs прошлая), месяц (vs прошлый). Чистые strength/growth/direction
тестируются без БД; подборка по тренду = топ-истории с этим тегом за окно (как дайджест).
"""

from __future__ import annotations

from sqlalchemy import text

from homyak.storage.db import SessionFactory

# period -> (hours окна, base_hours базы, base_div, min_count).
# База = средний ТИПИЧНЫЙ период до текущего (день vs средний день прошлой недели и т.п.) —
# устойчивее, чем «ровно предыдущее окно» (у фида ночью провал → шум day-over-day).
PERIODS = {
    "day": (24, 24 * 7, 7, 3),
    "week": (24 * 7, 24 * 28, 4, 5),
    "month": (24 * 30, 24 * 60, 2, 8),
}


def growth(count: float, prev: float) -> float:
    """Рост против типичной базы. Новая тема (prev≈0) → умеренный буст, не бесконечность."""
    if prev <= 0:
        return 2.0 if count > 0 else 0.0
    return (count - prev) / prev


def strength(count: float, prev: float, avg_score: float | None) -> float:
    """Сила тренда — про МОМЕНТУМ, не про объём: объём сублинейно (count^0.6), рост в степени 1.5
    (спад приглушает, разгон усиливает; клампы 0.1..4), взвешено релевантностью."""
    g = growth(count, prev)
    vol = count**0.6
    mom = min(max(1.0 + g, 0.1), 4.0) ** 1.5
    rel = 0.4 + 0.6 * (avg_score or 0.0)
    return vol * mom * rel


def direction(count: float, prev: float) -> str:
    """↑ разгон · → ровно · ↓ спад."""
    g = growth(count, prev)
    return "↑" if g >= 0.25 else ("↓" if g <= -0.25 else "→")


async def compute_trends(period: str = "day", limit: int = 8) -> list[dict]:
    """Топ трендовых тем за период. {tag, count, prev, growth, avg_score, direction, strength}."""
    hours, base_hours, base_div, min_count = PERIODS.get(period, PERIODS["day"])
    async with SessionFactory() as s:
        rows = (
            await s.execute(
                text(
                    "with cur as ("
                    "  select tag, count(*) c, avg(personal_score) s from ("
                    "    select unnest(tags) tag, personal_score from news_items"
                    "    where processed_at is not null and skip_reason is null"
                    "      and personal_score is not null"
                    "      and coalesce(published_at,fetched_at) > now() - make_interval(hours=>:h)"
                    "  ) t group by tag"
                    "),"
                    "base as ("  # средний типичный период до текущего окна
                    "  select tag, count(*)::float / :bdiv c from ("
                    "    select unnest(tags) tag from news_items"
                    "    where processed_at is not null and skip_reason is null"
                    "      and coalesce(published_at,fetched_at) <= now() - make_interval(hours=>:h)"
                    "      and coalesce(published_at,fetched_at) > now() - make_interval(hours=>:total)"
                    "  ) t group by tag"
                    ")"
                    " select cur.tag, cur.c, coalesce(base.c,0) pc, cur.s"
                    " from cur left join base on base.tag = cur.tag"
                    " where cur.c >= :minc"
                ),
                {"h": hours, "total": hours + base_hours, "bdiv": base_div, "minc": min_count},
            )
        ).all()
    trends = [
        {
            "tag": r.tag,
            "count": r.c,
            "prev": r.pc,
            "growth": round(min(growth(r.c, r.pc), 9.99), 2),  # cap: у молодых данных база ≈0
            "avg_score": round(float(r.s), 3) if r.s is not None else None,
            "direction": direction(r.c, r.pc),
            "strength": strength(r.c, r.pc, r.s),
        }
        for r in rows
        if r.tag
    ]
    trends.sort(key=lambda t: t["strength"], reverse=True)
    return trends[:limit]


async def trend_items(tag: str, period: str = "day", limit: int = 10) -> list[dict]:
    """Подборка по тренду: топ-истории с этим тегом за окно (schema как у дайджеста)."""
    hours = PERIODS.get(period, PERIODS["day"])[0]
    async with SessionFactory() as s:
        raw = (
            await s.execute(
                text(
                    "select id, title, source_type, feed_name, vertical, personal_score, summary, url,"
                    " cluster_id, tags,"
                    " extract(epoch from (now()-coalesce(published_at,fetched_at)))::int age_s"
                    " from news_items"
                    " where processed_at is not null and skip_reason is null"
                    "   and personal_score is not null and tags @> array[:tag]::text[]"
                    "   and coalesce(published_at,fetched_at) > now() - make_interval(hours=>:h)"
                    " order by personal_score desc limit :win"
                ),
                {"tag": tag, "h": hours, "win": limit * 3},
            )
        ).all()
    seen: set[int] = set()
    out: list[dict] = []
    for r in raw:
        key = r.cluster_id or -r.id
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": r.id,
                "title": r.title,
                "feed": r.feed_name,
                "vertical": r.vertical,
                "score": r.personal_score,
                "summary": (r.summary or "")[:200] or None,
                "url": r.url,
                "tags": list(r.tags or [])[:4],
                "age_s": r.age_s,
            }
        )
        if len(out) >= limit:
            break
    return out
