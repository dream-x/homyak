"""Дайджест «самого интересного за период»: топ по personal_score за N часов, схлоп кластеров,
плюс короткая LLM-вводка «что было главное». Используется кнопкой/командами бота и авто-недельным лупом.
"""

from __future__ import annotations

from sqlalchemy import text

from homyak.core.config import settings
from homyak.core.llm import OllamaLLM
from homyak.storage.db import SessionFactory

_INTRO_SYSTEM = (
    "Ты редактор персонального дайджеста. По списку топ-новостей за период напиши 2–3 предложения "
    "на русском: что было главное, какие темы задавали тон. Только по списку, без домыслов, без "
    "преамбулы и хэштегов. Живо и по делу."
)


async def top_of_period(hours: int, limit: int = 12) -> list[dict]:
    """Топ записей по personal_score за последние N часов, кластеры схлопнуты (одна история — раз)."""
    async with SessionFactory() as s:
        raw = (
            await s.execute(
                text(
                    "select id, title, source_type, feed_name, vertical, personal_score, summary, url,"
                    " cluster_id, tags,"
                    " extract(epoch from (now()-coalesce(published_at,fetched_at)))::int age_s"
                    " from news_items"
                    " where processed_at is not null and skip_reason is null"
                    "   and personal_score is not null"
                    "   and coalesce(published_at, fetched_at) > now() - make_interval(hours => :h)"
                    " order by personal_score desc"
                    " limit :win"
                ),
                {"h": int(hours), "win": limit * 3},
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


async def _intro(items: list[dict], llm: OllamaLLM | None) -> str | None:
    lines = [f"- {it['title']} ({it.get('summary') or ''})"[:200] for it in items[:12]]
    try:
        raw = await (
            llm or OllamaLLM(model=settings.summary_model, fallback=settings.summary_fallback_model)
        ).chat_text(_INTRO_SYSTEM, "Новости:\n" + "\n".join(lines), think=False)
        return (raw or "").strip() or None
    except Exception:
        return None


async def build_digest(hours: int, limit: int = 12, llm: OllamaLLM | None = None) -> dict:
    """{hours, n, items, intro} — топ за период + LLM-вводка (best-effort)."""
    items = await top_of_period(hours, limit)
    intro = await _intro(items, llm) if items else None
    return {"hours": hours, "n": len(items), "items": items, "intro": intro}
