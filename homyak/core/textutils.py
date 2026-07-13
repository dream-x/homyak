"""Очистка текста из RSS/HTML — чистая функция, легко тестируется."""

from __future__ import annotations

import html as _html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# бесполезные «тексты» вместо тела статьи (HN даёт только ссылку «Comments» и т.п.)
_JUNK = {
    "comments",
    "comment",
    "read more",
    "read the rest",
    "continue reading",
    "…",
    "...",
    "[link]",
    "link",
}


def strip_html(raw: str | None) -> str | None:
    """HTML → плоский текст: убирает теги, распаковывает entity, схлопывает пробелы.

    Возвращает None, если после очистки пусто или это junk-заглушка (HN «Comments» и пр.).
    """
    if not raw:
        return None
    text = _TAG.sub(" ", raw)
    text = _html.unescape(text)
    text = _WS.sub(" ", text).strip()
    if not text or text.lower() in _JUNK:
        return None
    return text


def clean_title(raw: str | None) -> str | None:
    """Заголовок → плоский текст: срезает теги, распаковывает entity, схлопывает пробелы.

    В отличие от strip_html НЕ зануляет junk-слова — заголовок оставляем как есть,
    если после очистки он непустой (некоторые RSS кладут <a>…</a> прямо в <title>).
    """
    if not raw:
        return None
    text = _TAG.sub(" ", raw)
    text = _html.unescape(text)
    text = _WS.sub(" ", text).strip()
    return text or None
