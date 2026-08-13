"""Отдельный компонент: скачивание и извлечение полного текста статьи по URL.

RSS часто отдаёт только огрызок (а HN — вообще ссылку «Comments»). Тянем страницу и вытаскиваем
основной текст через trafilatura (топовый экстрактор). Если сайт блокирует бота (403) или рендерит
контент через JS — фолбэк на reader-сервис (r.jina.ai), который тянет и чистит страницу серверно.
Best-effort — при любой проблеме None.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx
import structlog

from homyak.core.config import settings
from homyak.core.github import fetch_readme

try:
    import trafilatura
except Exception:  # pragma: no cover
    trafilatura = None

log = structlog.get_logger(__name__)

# Реальный браузерный UA — кастомный homyak/0.1 многие сайты режут как бота (403).
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_MIN_CHARS = 400  # ниже — считаем извлечение неудачным, пробуем reader-фолбэк
_SKIP_HOSTS = tuple(h.strip().lower() for h in settings.article_skip_hosts.split(",") if h.strip())


def _skipped(url: str) -> bool:
    # Матч по границе хоста, НЕ по подстроке: иначе 'x.com' поймает 'phoronix.com'/'netflix.com'.
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS)


def _strip_reader(md: str | None) -> str | None:
    """r.jina.ai отдаёт 'Title: …\\nURL Source: …\\nMarkdown Content:\\n<текст>' — берём тело."""
    if not md:
        return None
    marker = "Markdown Content:"
    idx = md.find(marker)
    body = md[idx + len(marker) :] if idx != -1 else md
    return body.strip() or None


async def _fetch_html(url: str, timeout: float) -> str | None:
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=_HEADERS
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        log.debug("article_fetch_failed", url=url, error=str(e))
        return None


async def _reader_fallback(url: str, timeout: float) -> str | None:
    """Reader-сервис: тянет и чистит страницу серверно (обходит бот-блок/JS)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get("https://r.jina.ai/" + url, headers={"User-Agent": _UA})
            resp.raise_for_status()
            return _strip_reader(resp.text)
    except Exception as e:
        log.debug("article_reader_failed", url=url, error=str(e))
        return None


async def fetch_article(url: str, timeout: float = 20.0) -> str | None:
    """Скачивает URL и извлекает основной текст. Фолбэк на reader при неудаче. None при провале."""
    if not url or _skipped(url):  # известная бот-стена — не тратим попытки
        return None

    # Репозиторий — особый случай: у страницы GitHub общий экстрактор берёт обвязку вместо
    # содержания, поэтому README запрашиваем напрямую. Здесь, а не в ⭐-канале: полный текст
    # нужен всем — теггеру, саммари, судье, поиску и вике.
    readme = await fetch_readme(url, timeout)
    if readme and len(readme) >= _MIN_CHARS:
        return readme

    text = ""
    if trafilatura is not None:
        html = await _fetch_html(url, timeout)
        if html:
            try:
                extracted = await asyncio.to_thread(
                    trafilatura.extract,
                    html,
                    include_comments=False,
                    include_tables=False,
                    favor_recall=True,
                )
                text = (extracted or "").strip()
            except Exception as e:
                log.debug("article_extract_failed", url=url, error=str(e))

    # бот-блок (403) / JS-рендер → пробуем reader, берём если он дал больше
    if len(text) < _MIN_CHARS and settings.article_reader_fallback:
        reader = await _reader_fallback(url, timeout + 10)
        if reader and len(reader) > len(text):
            text = reader

    return text or None
