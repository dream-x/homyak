"""Вывод заголовка из тела поста. Чистая функция — легко тестируется.

Нужно для источников без поля «заголовок» (Telegram-посты, твиты, иногда RSS): у них
title пуст, а первая строка текста — по сути готовый заголовок. Берём её, чистим от
markdown/ссылок/эмодзи и подрезаем до разумной длины по границе слова/предложения.
"""

from __future__ import annotations

import re

_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")  # [текст](url) -> текст
_URL = re.compile(r"https?://\S+")
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
