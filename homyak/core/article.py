"""Отдельный компонент: скачивание и извлечение полного текста статьи по URL.

RSS часто отдаёт только огрызок (а HN — вообще одну ссылку «Comments»). Здесь тянем саму
страницу и вытаскиваем основной текст через trafilatura. Best-effort — при любой ошибке None.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

try:
    import trafilatura
except Exception:  # pragma: no cover
    trafilatura = None

log = structlog.get_logger(__name__)

_UA = "Mozilla/5.0 (compatible; homyak/0.1; +https://github.com/homyak)"


async def fetch_article(url: str, timeout: float = 20.0) -> str | None:
    """Скачивает URL и извлекает основной текст статьи. None при любой проблеме."""
    if not url or trafilatura is None:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        log.debug("article_fetch_failed", url=url, error=str(e))
        return None

    try:
        text = await asyncio.to_thread(
            trafilatura.extract,
            html,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception as e:
        log.debug("article_extract_failed", url=url, error=str(e))
        return None

    text = (text or "").strip()
    return text or None
