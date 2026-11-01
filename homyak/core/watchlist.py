"""Матчер трендовых тем: айтем → список тем, под которые он попал. Чистая логика, тестируется.

Только сопоставление. Откуда берутся сами темы — дело core/interests.py (секция `watch`
единственного файла интересов). Здесь нет ни settings, ни чтения файлов: матчер зовётся
на каждый айтем, и его хочется гонять в тестах без конфига и без БД.

Одиночный токен матчится префиксом по границе слова (\\bнефт → «нефть/нефтяной»); фраза со
пробелом/слэшем — по вхождению. Регистронезависимо, Unicode (Cyrillic \\b работает в Python re).

Тема может быть ограничена вертикалями (`verticals: [business, medical]`): английские алиасы
стихии (storm, tornado, cyclone, flood) в IT — это Storm ORM, Tornado, Cyclone DDS и защита от
flood'а, и README репозиториев получали метку «Природные явления». Вертикаль записи ставит
LLM-теггер (стадия 4), матчер (стадия 2, до префильтра) её не знает — поэтому `match` метит всё,
а снимает лишнее отдельная стадия `watch_verticals` после теггера (`restrict_to_verticals`).
Неизвестная вертикаль (теггер отнёс к other — так у большинства записей о стихии) метку не снимает.
"""

from __future__ import annotations

import re

# (name, [(is_phrase, needle_or_regex)], verticals | None) — предкомпилировано; None = любая
CompiledTopic = tuple[str, list[tuple[bool, object]], frozenset[str] | None]


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
        verts = t.get("verticals") or None
        if name and aliases:
            out.append((
                name,
                [_compile_alias(a) for a in aliases],
                frozenset(str(v).lower() for v in verts) if verts else None,
            ))
    return out


def match(title: str | None, text: str | None, compiled: list[CompiledTopic]) -> list[str]:
    """Список имён тем, под которые попадает айтем (без учёта вертикали — она ещё неизвестна)."""
    hay = f"{title or ''} {text or ''}"[:4000].lower()
    hits: list[str] = []
    for name, aliases, _verts in compiled:
        for is_phrase, needle in aliases:
            if (needle in hay) if is_phrase else needle.search(hay):
                hits.append(name)
                break
    return hits


def restrict_to_verticals(
    hits: list[str], vertical: str | None, compiled: list[CompiledTopic]
) -> list[str]:
    """Снять с записи темы, ограниченные вертикалями, если её вертикаль известна и не в списке.
    None (теггер отнёс к other) — не повод снимать: стихию он чаще всего относит именно туда."""
    if not hits or vertical is None:
        return list(hits)
    limits = {name: verts for name, _, verts in compiled}
    return [h for h in hits if limits.get(h) is None or vertical in limits[h]]
