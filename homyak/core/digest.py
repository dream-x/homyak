"""Дайджест «самого интересного за период»: топ по personal_score за N часов, схлоп кластеров,
плюс короткая LLM-вводка «что было главное». Используется кнопкой/командами бота и авто-недельным лупом.
"""

from __future__ import annotations

from sqlalchemy import text

from homyak.core.config import settings
from homyak.core.llm import OllamaLLM
from homyak.storage.db import SessionFactory

_COMPOSE_SYSTEM = (
    "Ты ведёшь персональный дайджест для коллеги-инженера. На входе — пронумерованный список новостей "
    "(часть на английском). Верни СТРОГО JSON:\n"
    '{"intro": "...", "items": [{"n": 1, "desc": "..."}]}\n\n'
    "intro — сводка ОДНИМ абзацем, 3–4 предложения, только 2–3 главные темы периода. Живой язык, "
    "конкретика (продукты, компании, цифры). Без канцелярита, без «в мире технологий», «стоит отметить».\n"
    "desc — ОДНА строка до 100 символов: что это и чем полезно. Для каждого номера из списка.\n\n"
    "ВСЁ — на русском, даже если новость на английском (названия продуктов оставляй как есть). "
    "Строго по списку, ничего не выдумывай."
)


async def top_of_period(hours: int, limit: int = 12, exclude: list[int] | None = None) -> list[dict]:
    """Топ записей по personal_score за последние N часов, кластеры схлопнуты (одна история — раз).

    exclude — id, показанные в прошлом дайджесте: топ по скору статичен, и без этого повторный
    вызов за тот же день отдаёт ровно тот же список.
    """
    async with SessionFactory() as s:
        raw = (
            await s.execute(
                text(
                    "select id, title, source_type, feed_name, vertical, personal_score, summary, url,"
                    " cluster_id, tags,"
                    " coalesce(published_at,fetched_at) published, extract(epoch from (now()-coalesce(published_at,fetched_at)))::int age_s"
                    " from news_items"
                    " where processed_at is not null and skip_reason is null"
                    "   and personal_score is not null"
                    "   and coalesce(published_at, fetched_at) > now() - make_interval(hours => :h)"
                    " order by personal_score desc"
                    " limit :win"
                ),
                {"h": int(hours), "win": limit * 3 + len(exclude or [])},
            )
        ).all()
    skip = set(exclude or [])
    seen: set[int] = set()
    out: list[dict] = []
    for r in raw:
        if r.id in skip:
            continue
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
                "summary": (r.summary or "")[:400] or None,
                "url": r.url,
                "tags": list(r.tags or [])[:4],
                "published": r.published,
                "age_s": r.age_s,
            }
        )
        if len(out) >= limit:
            break
    return out


async def _compose(items: list[dict], llm: OllamaLLM | None) -> tuple[str | None, dict[int, str]]:
    """Один вызов LLM: сводка + короткие русские описания к пунктам (единый язык дайджеста)."""
    lines = []
    for i, it in enumerate(items, 1):
        body = (it.get("summary") or "").replace("\n", " ").strip()[:320]
        src = it.get("feed") or ""
        lines.append(f"{i}. {it['title']}" + (f" [{src}]" if src else "") + (f": {body}" if body else ""))
    try:
        data = await (
            llm or OllamaLLM(model=settings.summary_model, fallback=settings.summary_fallback_model)
        ).chat_json(_COMPOSE_SYSTEM, "Новости:\n" + "\n".join(lines))
        intro = (data.get("intro") or "").strip() or None
        descs: dict[int, str] = {}
        for row in data.get("items") or []:
            try:
                descs[int(row["n"])] = str(row["desc"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
        return intro, descs
    except Exception:
        return None, {}


async def build_digest(hours: int, limit: int = 12, llm: OllamaLLM | None = None,
                       exclude: list[int] | None = None) -> dict:
    """{hours, n, items, intro} — топ за период + сводка и описания на одном языке (best-effort)."""
    items = await top_of_period(hours, limit, exclude)
    intro, descs = (await _compose(items, llm)) if items else (None, {})
    for i, it in enumerate(items, 1):
        if i in descs:  # LLM не ответила по пункту → остаётся исходное саммари
            it["summary"] = descs[i]
    return {"hours": hours, "n": len(items), "items": items, "intro": intro}
