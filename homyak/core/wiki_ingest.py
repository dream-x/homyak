"""Ingest в LLM-вику: сохранённая запись (⭐/👍) → страницы концептов/сущностей + source + log.

LLM извлекает из записи ключевые концепты (идеи/технологии) и сущности (люди/компании/инструменты)
с однострочным тейкэвеем; мы мерджим их в связанные страницы датированными буллетами и
[[wikilink]]-ссылками (Obsidian-нативно). Идемпотентно по source-ref: повторный ⭐ не задваивает.
Best-effort: если LLM упал — source-страница и log всё равно пишутся.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from homyak.core.config import settings
from homyak.core.llm import OllamaLLM
from homyak.core.textutils import detect_lang, strip_html
from homyak.core import wiki
from homyak.core.wiki import slugify

log = structlog.get_logger(__name__)

_EXTRACT = (
    "From the item below, extract the key CONCEPTS (ideas, technologies, frameworks) and ENTITIES "
    "(people, companies, tools) it is really about, plus a one-sentence takeaway for each. "
    "Names must be short and canonical ({lang}). Return STRICT JSON: "
    '{{"concepts":[{{"name":"...","note":"..."}}],"entities":[{{"name":"...","note":"..."}}]}}. '
    "At most 5 concepts and 5 entities, no fluff, only what the item genuinely covers."
)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _merge_page(kind: str, name: str, note: str, source_ref: str, date: str) -> str | None:
    """Создать/дополнить страницу концепта/сущности датированным буллетом со ссылкой на source."""
    name = (name or "").strip()
    if not name:
        return None
    slug = slugify(name)
    link = f"[[{source_ref}]]"
    body = wiki.read_page(kind, slug)
    if body is None:
        body = f"# {name}\n\n{note.strip()}\n\n## Упоминания\n"
    if link in body:  # уже отмечали этот источник — не задваиваем
        return slug
    bullet = f"- [{date}] {note.strip()} — {link}"
    wiki.write_page(kind, slug, body.rstrip() + "\n" + bullet)
    return slug


def _write_source(item, date: str) -> str:
    ref = f"sources/{item.id}-{slugify(item.title or 'source')}"
    body = (item.summary or strip_html(item.text) or "").strip()
    feed = item.feed_name or item.source_type or "—"
    tags = " ".join(f"#{t}" for t in (item.tags or [])[:6])
    url = item.url or ""
    content = (
        f"# {item.title or '(без заголовка)'}\n\n"
        f"- сохранено: {date} · источник: {feed}\n"
        f"{('- ' + url) if url else ''}\n"
        f"{(tags + chr(10)) if tags else ''}\n"
        f"{body}\n"
    )
    wiki.write_page("sources", ref.split("/", 1)[1], content)
    return ref


async def ingest_item(item, llm: OllamaLLM | None = None) -> dict:
    """Записать/обновить страницы вики по одной сохранённой записи. Возвращает сводку изменений."""
    wiki.ensure_dirs()
    date = _today()
    source_ref = _write_source(item, date)

    lang = "Russian" if detect_lang(item.summary or item.text or item.title) == "ru" else "English"
    text_for_llm = (item.summary or strip_html(item.text) or "")[:3000]
    user = f"Title: {item.title or '—'}\n\n{text_for_llm}"
    concepts_done: list[str] = []
    entities_done: list[str] = []
    try:
        data = await (llm or OllamaLLM()).chat_json(_EXTRACT.format(lang=lang), user)
        for c in (data.get("concepts") or [])[:5]:
            slug = _merge_page("concepts", c.get("name", ""), c.get("note", ""), source_ref, date)
            if slug:
                concepts_done.append(slug)
        for e in (data.get("entities") or [])[:5]:
            slug = _merge_page("entities", e.get("name", ""), e.get("note", ""), source_ref, date)
            if slug:
                entities_done.append(slug)
    except Exception as e:  # best-effort — source и log остаются
        log.warning("wiki_ingest_llm_failed", item=getattr(item, "id", None), error=str(e)[:150])

    wiki.append("log.md", f"## [{date}] ingest | {item.title or source_ref}")
    st = wiki.stats()
    log.info(
        "wiki_ingested",
        item=getattr(item, "id", None),
        concepts=len(concepts_done),
        entities=len(entities_done),
        pages=st,
    )
    return {"source": source_ref, "concepts": concepts_done, "entities": entities_done}
