"""Вывод заголовка из тела поста. Чистая функция — легко тестируется.

Нужно для источников без поля «заголовок» (Telegram-посты, твиты, иногда RSS): у них
title пуст, а первая строка текста — по сути готовый заголовок. Берём её, чистим от
markdown/ссылок/эмодзи и подрезаем до разумной длины по границе слова/предложения.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")  # [текст](url) -> текст
_URL = re.compile(r"https?://\S+")
# «мусорные» сегменты пути: смысла в заголовке не несут
_URL_STOP = {
    "blog", "post", "posts", "article", "articles", "news", "p", "index",
    "en", "ru", "ru-ru", "en-us", "story", "read", "amp", "www",
}
_MD_MARKS = re.compile(r"[*_`~]{1,3}")  # **bold** _italic_ `code` ~strike~
_WS = re.compile(r"[ \t ]+")
# ведущий мусор: эмодзи, буллеты, стрелки, разделители — до первого «содержательного» символа
_LEAD_JUNK = re.compile(r"^[\s\W_]*?(?=[\w\"'«(])", re.UNICODE)
_SENT_END = re.compile(r"[.!?…](?:\s|$)")

MAX_LEN = 90


def derive_title(text: str | None, *, fallback: str | None = None) -> str | None:
    """Заголовок из первой смысловой строки текста. None (или fallback), если текста нет.

    Идемпотентна для уже коротких однострочных заголовков. Не выдумывает содержание —
    только вырезает первую строку и подрезает, ссылки/markdown/эмодзи убирает.
    """
    raw = (text or "").strip()
    if not raw:
        return fallback

    # первый непустой абзац → первая его строка
    line = ""
    for chunk in raw.replace("\r", "").split("\n"):
        chunk = chunk.strip()
        if chunk:
            line = chunk
            break
    if not line:
        return fallback

    line = _MD_LINK.sub(r"\1", line)
    line = _URL.sub("", line)
    line = _MD_MARKS.sub("", line)
    line = _WS.sub(" ", line).strip()
    line = _LEAD_JUNK.sub("", line).strip()
    if not line:
        return fallback

    if len(line) <= MAX_LEN:
        return line

    # длинная строка: режем по концу предложения, если он рядом, иначе по границе слова
    m = _SENT_END.search(line, 0, MAX_LEN + 30)
    if m and m.start() >= 20:
        return line[: m.start() + 1].strip()
    cut = line[:MAX_LEN].rsplit(" ", 1)[0].strip() or line[:MAX_LEN].strip()
    return cut.rstrip(" ,;:—-") + "…"


def title_from_url(url: str | None) -> str | None:
    """Читаемый заголовок из слага URL — для постов-голых-ссылок без текста.

    Берёт последний осмысленный сегмент пути (пропуская blog/news/цифровые id),
    заменяет -/_ на пробелы, снимает расширение. github.com/o/repo → «repo».
    None, если вытащить нечего (домен-корень).
    """
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return None
    segs = [unquote(s) for s in parts.path.split("/") if s]
    while segs and (segs[-1].lower() in _URL_STOP or segs[-1].isdigit()):
        segs.pop()
    if not segs:
        return None
    slug = re.sub(r"\.(html?|php|aspx?)$", "", segs[-1], flags=re.IGNORECASE)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    if not slug:
        return None
    slug = slug[0].upper() + slug[1:]
    return slug if len(slug) <= MAX_LEN else slug[:MAX_LEN].rsplit(" ", 1)[0] + "…"
