"""LLM-вика (по мотивам Karpathy LLM Wiki): компаундящаяся markdown-база из ⭐/👍.

НЕ RAG: сервис homyak-wiki читает сохранённые записи и инкрементально ведёт связанные страницы —
концепты, сущности, саммари источников, index, log. Здесь — только хранилище (файлы) + slugify
(чистая функция, тестируется). Логика ingest/query/lint — в wiki_ingest/wiki_query.

Раскладка каталога (settings.wiki_dir, смонтирован volume'ом, смотрится в Obsidian):
  index.md              — каталог страниц по категориям
  log.md                — append-only хронология (## [YYYY-MM-DD] ingest | …)
  lint.md               — последний отчёт аудита
  concepts/<slug>.md    — идея/технология/фреймворк
  entities/<slug>.md    — человек/компания/инструмент
  sources/<id>-<slug>.md — саммари конкретной сохранённой записи
"""

from __future__ import annotations

import re
from pathlib import Path

from homyak.core.config import settings

_SLUG_BAD = re.compile(r"[^0-9a-zA-Zа-яёА-ЯЁ]+")
KINDS = ("concepts", "entities", "sources")


def slugify(name: str, *, maxlen: int = 60) -> str:
    """Имя → безопасный слаг для файла: lower, не-буквы/цифры → '-', без хвостовых '-'.

    Кириллицу оставляем (Linux-fs её держит) — читаемее, чем транслит. Пусто → 'untitled'.
    """
    s = _SLUG_BAD.sub("-", (name or "").strip().lower()).strip("-")
    return (s[:maxlen].strip("-") or "untitled")


def wiki_root() -> Path:
    return Path(settings.wiki_dir)


def ensure_dirs() -> None:
    root = wiki_root()
    for k in KINDS:
        (root / k).mkdir(parents=True, exist_ok=True)
    for f in ("index.md", "log.md"):
        p = root / f
        if not p.exists():
            p.write_text(f"# {f[:-3]}\n\n", encoding="utf-8")


def page_path(kind: str, slug: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"wiki: неизвестный тип страницы {kind!r}")
    return wiki_root() / kind / f"{slug}.md"


def read_page(kind: str, slug: str) -> str | None:
    p = page_path(kind, slug)
    return p.read_text(encoding="utf-8") if p.exists() else None


def write_page(kind: str, slug: str, content: str) -> None:
    ensure_dirs()
    page_path(kind, slug).write_text(content.rstrip() + "\n", encoding="utf-8")


def append(rel: str, line: str) -> None:
    """Дописать строку в index.md/log.md (создаёт при отсутствии)."""
    ensure_dirs()
    p = wiki_root() / rel
    with p.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def list_pages(kind: str) -> list[str]:
    d = wiki_root() / kind
    return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []


def stats() -> dict[str, int]:
    return {k: len(list_pages(k)) for k in KINDS}


def run_lint() -> dict:
    """Детерминированный аудит: считает слабо-связанные концепт/сущность-страницы (одно упоминание)
    и пишет отчёт в lint.md. Без LLM — дёшево, можно гонять хоть каждый час."""
    ensure_dirs()
    weak: list[str] = []
    for kind in ("concepts", "entities"):
        for slug in list_pages(kind):
            body = read_page(kind, slug) or ""
            mentions = body.count("\n- [")  # датированные буллеты «- [YYYY-MM-DD] …»
            if mentions <= 1:
                weak.append(f"{kind}/{slug} ({mentions})")
    st = stats()
    lines = [
        f"# lint · {sum(st.values())} страниц",
        "",
        f"- concepts: {st['concepts']} · entities: {st['entities']} · sources: {st['sources']}",
        f"- слабо связанные (≤1 упоминание): {len(weak)}",
        "",
        "## Слабо связанные страницы",
        *(f"- {w}" for w in weak),
        "",
    ]
    (wiki_root() / "lint.md").write_text("\n".join(lines), encoding="utf-8")
    return {"pages": st, "weak": len(weak)}
