"""Вотчлист трендовых тем: матч айтема на темы по алиасам (RU+EN). Чистая логика, тестируется.

Одиночный токен матчится префиксом по границе слова (\\bнефт → «нефть/нефтяной»); фраза со
пробелом/слэшем — по вхождению. Регистронезависимо, Unicode (Cyrillic \\b работает в Python re).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from homyak.core.config import settings

# (name, [(is_phrase, needle_or_regex)]) — предкомпилировано
_Topic = tuple[str, list[tuple[bool, object]]]


def _compile_alias(alias: str) -> tuple[bool, object]:
    a = alias.lower().strip()
    if " " in a or "/" in a:  # фраза → вхождение
        return True, a
    return False, re.compile(r"\b" + re.escape(a), re.IGNORECASE | re.UNICODE)


def compile_watchlist(topics: list[dict]) -> list[_Topic]:
    out: list[_Topic] = []
    for t in topics or []:
        name = t.get("name")
        aliases = t.get("aliases") or []
        if name and aliases:
            out.append((name, [_compile_alias(a) for a in aliases]))
    return out


def match(title: str | None, text: str | None, compiled: list[_Topic]) -> list[str]:
    """Список имён тем, под которые попадает айтем."""
    hay = f"{title or ''} {text or ''}"[:4000].lower()
    hits: list[str] = []
    for name, aliases in compiled:
        for is_phrase, needle in aliases:
            if (needle in hay) if is_phrase else needle.search(hay):
                hits.append(name)
                break
    return hits


@lru_cache(maxsize=1)
def load_watchlist(path: str | None = None) -> list[_Topic]:
    p = Path(path or getattr(settings, "watchlist_path", "config/watchlist.yaml"))
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return compile_watchlist(data.get("watchlist") or [])


def topic_names(path: str | None = None) -> list[str]:
    return [name for name, _ in load_watchlist(path)]
