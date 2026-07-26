"""Очистка текста из RSS/HTML — чистая функция, легко тестируется."""

from __future__ import annotations

import html as _html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CYR = re.compile(r"[а-яёА-ЯЁ]")
_LAT = re.compile(r"[a-zA-Z]")


def fmt_age(seconds: int | float | None) -> str:
    """Возраст новости человеку: «только что · 5м · 3ч · 2д · 3нед». Пусто, если неизвестно."""
    if seconds is None:
        return ""
    s = max(0, int(seconds))
    if s < 60:
        return "только что"
    m = s // 60
    if m < 60:
        return f"{m}м"
    h = m // 60
    if h < 24:
        return f"{h}ч"
    d = h // 24
    if d < 14:
        return f"{d}д"
    return f"{d // 7}нед"


_RU_MON = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def fmt_when(dt, now=None) -> str:
    """Фактическое время новости в ЛОКАЛЬНОЙ зоне процесса (TZ в compose): «14:32» если сегодня,
    «26 июл, 14:32» иначе, «26 июл 2025, 14:32» для прошлых лет. Пусто, если даты нет."""
    if dt is None:
        return ""
    from datetime import datetime, timezone

    if dt.tzinfo is None:  # naive из БД → считаем UTC
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    cur = (now or datetime.now(timezone.utc)).astimezone()
    hm = local.strftime("%H:%M")
    if local.date() == cur.date():
        return hm
    day = f"{local.day} {_RU_MON[local.month - 1]}"
    if local.year != cur.year:
        day += f" {local.year}"
    return f"{day}, {hm}"


def detect_lang(text: str | None) -> str:
    """Язык для генерации: 'ru' если текст преимущественно кириллический, иначе 'en'.

    Правило: русский → ru, английский → en, прочие языки → en (или ru, если письмо
    кириллическое). Кириллица считается доминирующей уже при трети от латиницы — у русских
    статей часто много кода/терминов латиницей, но саммари всё равно должно быть русским.
    """
    s = text or ""
    cyr = len(_CYR.findall(s))
    lat = len(_LAT.findall(s))
    if cyr == 0:
        return "en"
    return "ru" if cyr * 3 >= lat else "en"

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


_HASH_BAD = re.compile(r"[^0-9a-zA-Zа-яА-ЯёЁ_]+")


def hashtags(tags: list[str] | None, limit: int = 5) -> str:
    """Теги → кликабельные #хэштеги для Telegram.

    Telegram понимает только буквы/цифры/подчёркивание и требует букву в начале:
    'ai-agents' → '#ai_agents', '3d-печать' → '#d_печать' отбрасываем (начинается с цифры).
    """
    out: list[str] = []
    for t in (tags or [])[:limit]:
        s = _HASH_BAD.sub("_", str(t).strip().lower()).strip("_")
        if not s or s[0].isdigit():
            continue
        tag = f"#{s}"
        if tag not in out:
            out.append(tag)
    return " ".join(out)


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
