"""Вика как объяснимая ссылка на карточке — без буста в ранкере.

У записи есть теги и заголовок; у вики — страницы concepts/entities, выращенные из того, что ты
сам отметил ⭐/👍. Если тег записи или сущность из заголовка уже есть в вике — на карточке
появляется «📚 в базе знаний: ollama, self-hosting» со ссылками. Это ответ на «почему мне это
показали» через твою же историю, а не ещё один вес в формуле: ранкер этого слоя не видит.

Сопоставление лексическое: slugify(тег / канон тега) и slugify(слово/биграмма заголовка) против
slug'ов страниц. Индекс страниц (slug, вес = число датированных пунктов, H1) — с TTL 5 минут.
"""

from __future__ import annotations

import re
import time

from homyak.core import wiki
from homyak.core.config import settings
from homyak.core.tags import normalize_tag

_WORD = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ+#.]{2,}")
_BULLET = re.compile(r"^- \[\d{4}-\d{2}-\d{2}\]", re.M)  # датированный пункт = один источник
_KINDS = ("entities", "concepts")
_TTL = 300.0
_cache: dict = {"at": 0.0, "root": None, "pages": {}}


def _read_page(path) -> dict:
    """Вес страницы = число датированных пунктов (сколько твоих ⭐/👍 в неё легло) + заголовок H1."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"n": 0, "title": path.stem.replace("-", " ")}
    head = text.lstrip().split("\n", 1)[0]
    title = head[2:].strip() if head.startswith("# ") else path.stem.replace("-", " ")
    return {"n": len(_BULLET.findall(text)), "title": title or path.stem}


def _index(ttl: float = _TTL) -> dict[str, dict[str, dict]]:
    root = wiki.wiki_root()
    now = time.monotonic()
    if _cache["root"] != str(root) or now - _cache["at"] > ttl:
        pages = {}
        for k in _KINDS:
            d = root / k
            pages[k] = {p.stem: _read_page(p) for p in d.glob("*.md")} if d.exists() else {}
        _cache.update(at=now, root=str(root), pages=pages)
    return _cache["pages"]


def page_url(kind: str, slug: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/wiki?kind={kind}&slug={slug}"


def related_pages(
    title: str | None, tags: list[str] | None, *, limit: int = 3, ttl: float = _TTL
) -> list[dict]:
    """Страницы вики, лексически совпавшие с тегами записи и сущностями из заголовка.

    Кандидаты: теги (сырое написание и канон; сущности раньше концептов), затем слова и биграммы
    заголовка (только сущности: концепт по одному слову заголовка — почти всегда ложный).
    Один slug — одна страница (entities/rust и concepts/rust не двоятся: берётся тяжелее).
    Порядок выдачи — по весу: страница с 35 твоими источниками объясняет больше, чем с одним.
    """
    idx = _index(ttl)
    if not any(idx.values()):
        return []
    found: dict[str, tuple[int, str, dict]] = {}  # slug → (порядок, kind, page)

    def add(kind: str, slug: str) -> None:
        page = idx[kind].get(slug) if slug else None
        if page is None:
            return
        cur = found.get(slug)
        if cur is None:
            found[slug] = (len(found), kind, page)
        elif page["n"] > cur[2]["n"]:
            found[slug] = (cur[0], kind, page)

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
    ranked = sorted(found.items(), key=lambda kv: (-kv[1][2]["n"], kv[1][0]))
    return [
        {"kind": kind, "slug": slug, "title": page["title"], "n": page["n"], "url": page_url(kind, slug)}
        for slug, (_, kind, page) in ranked[:limit]
    ]
