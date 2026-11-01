"""Тренды: какие темы (теги) «разгоняются» за период, какие затихают. Формирование тренда — здесь.

Тренд = тег с высокой силой в окне: сила = объём (сколько историй) × рост (насколько больше, чем
в прошлом таком же окне) × релевантность (средний personal_score — что тебе заходит, наверху).
Периоды: день (vs вчера), неделя (vs прошлая), месяц (vs прошлый). Чистые strength/growth/direction
и отбор (rank_trends/select_fading) тестируются без БД; подборка по тренду = топ-истории с этим
тегом за окно (как дайджест).

Три правила, без которых тренд врёт (ревью 31.10.2026):
- считаем ИСТОРИИ, а не строки: кластер = одна история, HN+Lobsters+TG об одном не утраивают тему;
- оба окна — по одному и тому же срезу (дошедшие до ранкера: personal_score не NULL), иначе
  текущее окно и база несопоставимы;
- рост — по ДОЛЕ темы в окне, а не по сырому числу: провал сбора (прокси лёг, бокс завис) иначе
  выглядит как спад всех тем разом, а возвращение — как всплеск всего.
Теги сводятся к каноническим (core.tags) до distinct по историям: llm/llms/
large-language-models — одна тема, и одна история с двумя написаниями — одна история. Канон
считается в Python по различным тегам окна и уходит в SQL картой написаний (`spelling_map`),
а не регэкспом на каждую строку; SQL-двойник формы остаётся для маленьких таблиц (affinity, мьюты).
"""

from __future__ import annotations

from sqlalchemy import text

from homyak.core.tags import normalize_tag, spelling_map, spellings_of
from homyak.storage import db

# period -> (hours окна, base_hours базы, base_div, min_count).
# База = средний ТИПИЧНЫЙ период до текущего (день vs средний день прошлой недели и т.п.) —
# устойчивее, чем «ровно предыдущее окно» (у фида ночью провал → шум day-over-day).
PERIODS = {
    "day": (24, 24 * 7, 7, 3),
    "week": (24 * 7, 24 * 28, 4, 5),
    "month": (24 * 30, 24 * 60, 2, 8),
}

# Слишком широкие теги (уровень вертикали/категории) — из трендов исключаем, чтобы вылезали
# узкие темы (rust, ai-agents, prompt-injection), а не вечные business/ai. Правится под себя.
_BROAD_TAGS = {
    "ai", "business", "it", "medical", "tech", "technology", "news", "startups", "software",
    "health", "healthcare", "finance", "markets", "economy", "science",
}

_VERTICALS = ("business", "it", "medical")
_ITEM_FILTER = (
    " processed_at is not null and skip_reason is null and personal_score is not null"
)


def growth(count: float, prev: float) -> float:
    """Рост против типичной базы. Новая тема (prev≈0) → умеренный буст, не бесконечность."""
    if prev <= 0:
        return 2.0 if count > 0 else 0.0
    return (count - prev) / prev


def share_growth(count: float, total: float, prev: float, prev_total: float) -> float:
    """Рост ДОЛИ темы: (count/total) против (prev/prev_total). Без итогов — обычный growth."""
    if total <= 0 or prev_total <= 0:
        return growth(count, prev)
    return growth(count / total, prev / prev_total)


def strength(count: float, prev: float, avg_score: float | None, g: float | None = None) -> float:
    """Сила тренда — про МОМЕНТУМ, не про объём: объём сублинейно (count^0.6), рост в степени 1.5
    (спад приглушает, разгон усиливает; клампы 0.1..4), взвешено релевантностью."""
    if g is None:
        g = growth(count, prev)
    vol = count**0.6
    mom = min(max(1.0 + g, 0.1), 4.0) ** 1.5
    rel = 0.4 + 0.6 * (avg_score or 0.0)
    return vol * mom * rel


def direction(count: float, prev: float, g: float | None = None) -> str:
    """↑ разгон · → ровно · ↓ спад."""
    if g is None:
        g = growth(count, prev)
    return "↑" if g >= 0.25 else ("↓" if g <= -0.25 else "→")


def merge_canonical(rows: list[tuple[str, float, float, float | None]]) -> dict[str, dict]:
    """Строки (tag, count, prev, avg_score) → по каноническому тегу, широкие теги вон.

    SQL (_windows) уже отдаёт канон и считает истории по нему; здесь свод — страховка для
    строк из других источников (счёт складывается, релевантность — среднее, взвешенное count).
    Пересечения множеств историй тут не видно, поэтому полагаться на этот свод для
    распознавания синонимов нельзя — только SQL.
    """
    merged: dict[str, dict] = {}
    for tag, c, pc, s in rows:
        canon = normalize_tag(tag)
        if not canon or canon in _BROAD_TAGS:
            continue
        m = merged.setdefault(canon, {"tag": canon, "count": 0.0, "prev": 0.0, "_ssum": 0.0, "_sn": 0.0})
        m["count"] += c or 0
        m["prev"] += pc or 0
        if s is not None and c:
            m["_ssum"] += float(s) * c
            m["_sn"] += c
    for m in merged.values():
        m["avg_score"] = round(m["_ssum"] / m["_sn"], 3) if m["_sn"] else None
        del m["_ssum"], m["_sn"]
    return merged


def _entry(m: dict, total: float, prev_total: float) -> dict:
    g = share_growth(m["count"], total, m["prev"], prev_total)
    return {
        "tag": m["tag"],
        "count": int(m["count"]),
        "prev": round(m["prev"], 1),
        "growth": round(max(min(g, 9.99), -1.0), 2),  # cap: у молодых данных база ≈0
        "avg_score": m["avg_score"],
        "direction": direction(m["count"], m["prev"], g),
        "strength": strength(m["count"], m["prev"], m["avg_score"], g),
    }


def rank_trends(merged: dict[str, dict], total: float, prev_total: float, min_count: int, limit: int) -> list[dict]:
    """Что разгоняется: темы с объёмом ≥ min_count, по силе."""
    out = [_entry(m, total, prev_total) for m in merged.values() if m["count"] >= min_count]
    out.sort(key=lambda t: t["strength"], reverse=True)
    return out[:limit]


def select_fading(
    merged: dict[str, dict], total: float, prev_total: float, min_prev: int, limit: int
) -> list[dict]:
    """Что затихло: тема была заметной (prev ≥ min_prev), а её доля упала вдвое и больше.

    Сортировка — по потерянному объёму: громкая тема, ушедшая в тишину, важнее мелкой.
    """
    out = [
        _entry(m, total, prev_total)
        for m in merged.values()
        if m["prev"] >= min_prev and share_growth(m["count"], total, m["prev"], prev_total) <= -0.5
    ]
    out.sort(key=lambda t: t["prev"] - t["count"], reverse=True)
    return out[:limit]


async def _windows(
    period: str, vertical: str | None, sf=None
) -> tuple[dict[str, dict], float, float, int]:
    """Счётчики историй по каноническим тегам за окно и базу + итоги (для доли) + min_count.

    Канон — в SQL до distinct: (история, канон) — одна строка, сколько бы написаний ни стояло на
    записях кластера. Релевантность темы — среднее по историям (max personal_score в истории).
    """
    hours, base_hours, base_div, min_count = PERIODS.get(period, PERIODS["day"])
    vcond = " and vertical = :v" if vertical in _VERTICALS else ""
    params: dict = {"h": hours, "total": hours + base_hours, "bdiv": base_div}
    if vcond:
        params["v"] = vertical
    cur_items = (
        "select coalesce(cluster_id, -id) story, tags, personal_score from news_items where"
        + _ITEM_FILTER + vcond
        + " and coalesce(published_at,fetched_at) > now() - make_interval(hours=>:h)"
    )
    base_items = (
        "select coalesce(cluster_id, -id) story, tags from news_items where"
        + _ITEM_FILTER + vcond
        + " and coalesce(published_at,fetched_at) <= now() - make_interval(hours=>:h)"
        + " and coalesce(published_at,fetched_at) > now() - make_interval(hours=>:total)"
    )
    # канон — по карте написаний окна (Python), в SQL — hash join по unnest(raws, canons)
    canon = "join unnest(cast(:raws as text[]), cast(:canons as text[])) m(raw, canon) on m.raw = t"
    async with (sf or db.SessionFactory)() as s:
        raws, canons = await spelling_map(
            s,
            _ITEM_FILTER + vcond
            + " and coalesce(published_at,fetched_at) > now() - make_interval(hours=>:total)",
            params,
        )
        params = {**params, "raws": raws, "canons": canons}
        rows = (
            await s.execute(
                text(
                    f"with cur_items as ({cur_items}), base_items as ({base_items}),"
                    " cur_tags as (select story, m.canon tag, max(personal_score) s"
                    f"   from cur_items, unnest(tags) t {canon} group by story, m.canon),"
                    " base_tags as (select distinct story, m.canon tag"
                    f"   from base_items, unnest(tags) t {canon}),"
                    " cur as (select tag, count(*) c, avg(s) s from cur_tags group by tag),"
                    " base as (select tag, count(*)::float / :bdiv c from base_tags group by tag)"
                    " select coalesce(cur.tag, base.tag) tag, coalesce(cur.c, 0) c,"
                    "   coalesce(base.c, 0) pc, cur.s"
                    " from cur full outer join base on base.tag = cur.tag"
                ),
                params,
            )
        ).all()
        totals = (
            await s.execute(
                text(
                    f"select (select count(distinct story) from ({cur_items}) a) tc,"
                    f" (select count(distinct story)::float / :bdiv from ({base_items}) b) tb"
                ),
                params,
            )
        ).one()
    merged = merge_canonical([(r.tag, float(r.c), float(r.pc), r.s) for r in rows if r.tag])
    return merged, float(totals.tc or 0), float(totals.tb or 0), min_count


async def trend_report(
    period: str = "day",
    vertical: str | None = None,
    limit: int = 8,
    fading_limit: int = 5,
    sf=None,
) -> tuple[list[dict], list[dict]]:
    """(что разгоняется, что затихло) за один проход по БД — окна общие для обоих списков.

    Элемент: {tag, count, prev, growth, avg_score, direction, strength}. vertical — ограничить
    одной вертикалью (business|it|medical); широкие теги (_BROAD_TAGS) вон. «Затихло» пустое,
    если трендов нет: при пустом окне любой спад — артефакт сбора, а не темы.
    """
    merged, total, prev_total, min_count = await _windows(period, vertical, sf)
    trends = rank_trends(merged, total, prev_total, min_count, limit)
    if not trends:
        return [], []
    return trends, select_fading(merged, total, prev_total, max(min_count, 3), fading_limit)


async def compute_trends(period: str = "day", vertical: str | None = None, limit: int = 8) -> list[dict]:
    """Только топ трендовых тем (см. trend_report)."""
    trends, _ = await trend_report(period, vertical, limit)
    return trends


async def compute_fading(period: str = "day", vertical: str | None = None, limit: int = 5) -> list[dict]:
    """Только затихшие темы (см. trend_report)."""
    _, fading = await trend_report(period, vertical, fading_limit=limit)
    return fading


async def trend_items(
    tag: str, period: str = "day", vertical: str | None = None, limit: int = 10, sf=None
) -> list[dict]:
    """Подборка по тренду: топ-истории с этим тегом за окно — в любом историческом написании
    (тот же SQL-канон, что считает тренд: что посчитано, то и показано)."""
    hours = PERIODS.get(period, PERIODS["day"])[0]
    vcond = " and vertical = :v" if vertical in _VERTICALS else ""
    params: dict = {"h": hours, "win": limit * 3}
    if vcond:
        params["v"] = vertical
    window = (
        _ITEM_FILTER + vcond
        + " and coalesce(published_at,fetched_at) > now() - make_interval(hours=>:h)"
    )
    async with (sf or db.SessionFactory)() as s:
        raws, canons = await spelling_map(s, window, params)
        spellings = spellings_of(raws, canons, [normalize_tag(tag) or tag])
        if not spellings:
            return []
        raw = (
            await s.execute(
                text(
                    "select id, title, source_type, feed_name, vertical, personal_score, summary,"
                    " url, cluster_id, tags, coalesce(published_at,fetched_at) published,"
                    " extract(epoch from (now()-coalesce(published_at,fetched_at)))::int age_s"
                    " from news_items where" + window +
                    "   and tags && cast(:spellings as text[])"  # GIN по tags
                    " order by personal_score desc limit :win"
                ),
                {**params, "spellings": spellings},
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
                "published": r.published,
                "age_s": r.age_s,
            }
        )
        if len(out) >= limit:
            break
    return out
