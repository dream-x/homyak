"""Вика как объяснимая ссылка на карточке — без буста в ранкере.

У записи есть теги и заголовок; у вики — страницы concepts/entities, выращенные из того, что ты
сам отметил ⭐/👍. Если тег записи или сущность из заголовка уже есть в вике — на карточке
появляется «📚 в базе знаний: ollama, self-hosting» со ссылками. Это ответ на «почему мне это
показали» через твою же историю, а не ещё один вес в формуле: ранкер этого слоя не видит.

Сопоставление лексическое: slugify(канон тега) и slugify(слово/биграмма заголовка) против
slug'ов страниц. Индекс страниц — листинг каталога с TTL, файлы заново не читаются.
"""

from __future__ import annotations

import re
import time

from homyak.core import wiki
from homyak.core.config import settings
from homyak.core.tags import normalize_tag

_WORD = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ+#.]{2,}")
_KINDS = ("entities", "concepts")
_TTL = 300.0
_cache: dict = {"at": 0.0, "root": None, "slugs": {}}


def _index(ttl: float = _TTL) -> dict[str, set[str]]:
    root = str(wiki.wiki_root())
    now = time.monotonic()
    if _cache["root"] != root or now - _cache["at"] > ttl:
        _cache.update(
            at=now, root=root, slugs={k: set(wiki.list_pages(k)) for k in _KINDS}
        )
    return _cache["slugs"]


def page_url(kind: str, slug: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/wiki?kind={kind}&slug={slug}"


def related_pages(
    title: str | None, tags: list[str] | None, *, limit: int = 3, ttl: float = _TTL
) -> list[dict]:
    """Страницы вики, лексически совпавшие с тегами записи и сущностями из заголовка.

    Порядок: теги (явные темы) — сущности раньше концептов, затем слова и биграммы заголовка
    (только сущности: концепт по одному слову заголовка — почти всегда ложный).
    """
    idx = _index(ttl)
    if not any(idx.values()):
        return []
    found: list[tuple[str, str]] = []

    def add(kind: str, slug: str) -> None:
        if slug and slug in idx[kind] and (kind, slug) not in found:
            found.append((kind, slug))

    for tag in tags or []:
        # и сырое написание, и канон: вика зовёт страницу так, как её назвала LLM
        # (self-hosting), а канон тега — selfhosted; совпасть может любое
        for form in {wiki.slugify(tag), wiki.slugify(normalize_tag(tag) or "")}:
            for kind in _KINDS:
                add(kind, form)
    words = [w.lower() for w in _WORD.findall(title or "")]
    for i, w in enumerate(words):
        if len(w) >= 3:
            add("entities", wiki.slugify(w))
        if i + 1 < len(words):
            add("entities", wiki.slugify(f"{w} {words[i + 1]}"))
    return [
        {"kind": kind, "slug": slug, "title": slug.replace("-", " "), "url": page_url(kind, slug)}
        for kind, slug in found[:limit]
    ]
