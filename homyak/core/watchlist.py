"""Матчер трендовых тем: айтем → список тем, под которые он попал. Чистая логика, тестируется.

Только сопоставление. Откуда берутся сами темы — дело core/interests.py (секция `watch`
единственного файла интересов). Здесь нет ни settings, ни чтения файлов: матчер зовётся
на каждый айтем, и его хочется гонять в тестах без конфига и без БД.

Одиночный токен матчится префиксом по границе слова (\\bнефт → «нефть/нефтяной»); фраза со
пробелом/слэшем — по вхождению. Регистронезависимо, Unicode (Cyrillic \\b работает в Python re).
"""

from __future__ import annotations

import re

# (name, [(is_phrase, needle_or_regex)]) — предкомпилировано
CompiledTopic = tuple[str, list[tuple[bool, object]]]


def _compile_alias(alias: str) -> tuple[bool, object]:
    a = alias.lower().strip()
    if " " in a or "/" in a:  # фраза → вхождение
        return True, a
    return False, re.compile(r"\b" + re.escape(a), re.IGNORECASE | re.UNICODE)


def compile_watchlist(topics: list[dict]) -> list[CompiledTopic]:
    out: list[CompiledTopic] = []
    for t in topics or []:
        name = t.get("name")
        aliases = t.get("aliases") or []
        if name and aliases:
            out.append((name, [_compile_alias(a) for a in aliases]))
    return out


def match(title: str | None, text: str | None, compiled: list[CompiledTopic]) -> list[str]:
    """Список имён тем, под которые попадает айтем."""
    hay = f"{title or ''} {text or ''}"[:4000].lower()
    hits: list[str] = []
    for name, aliases in compiled:
        for is_phrase, needle in aliases:
            if (needle in hay) if is_phrase else needle.search(hay):
                hits.append(name)
                break
    return hits
