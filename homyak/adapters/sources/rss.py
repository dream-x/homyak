"""Универсальный RSS/Atom source-адаптер (PollSource). Один инстанс на фид из sources.yaml."""

from __future__ import annotations

import calendar
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import structlog

from homyak.core.config import RSSFeedConfig, settings
from homyak.core.interfaces import NewsItemDTO
from homyak.core.textutils import clean_title, strip_html

log = structlog.get_logger(__name__)


def _effective_cutoff(
    cursor: str | None, interval_seconds: int, now: datetime, factor: float
) -> datetime:
    """Граница «прошлое не нужно»: не старше одного окна поллинга (interval * factor).

    Первый контакт с фидом → берём только текущее окно, а не всю выдачу (~20 старых твитов).
    После простоя (курсор протух) → не догоняем долги, стартуем от горизонта.
    В обычном режиме курсор свежее горизонта → работает он.
    """
    horizon = now - timedelta(seconds=interval_seconds * factor)
    if not cursor:
        return horizon
    try:
        c = datetime.fromisoformat(cursor)
    except ValueError:
        return horizon
    if c.tzinfo is None:
        c = c.replace(tzinfo=timezone.utc)
    return max(c, horizon)


def _entry_text(e) -> str | None:
    content = ""
    if getattr(e, "content", None):
        try:
            content = e.content[0].get("value", "")
        except Exception:
            content = ""
    raw = content or e.get("summary") or e.get("description") or ""
    return strip_html(raw)

_UA = "homyak/0.1 (+https://github.com/homyak)"


def _parsed_to_dt(st) -> datetime | None:
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def _entry_published(e) -> datetime | None:
    return _parsed_to_dt(getattr(e, "published_parsed", None)) or _parsed_to_dt(
        getattr(e, "updated_parsed", None)
    )


def _media(e) -> list[str]:
    urls: list[str] = []
    for m in getattr(e, "media_content", []) or []:
        if m.get("url"):
            urls.append(m["url"])
    for enc in getattr(e, "enclosures", []) or []:
        if enc.get("href"):
            urls.append(enc["href"])
    return urls


def _source_id(e) -> str:
    return e.get("id") or e.get("link") or f"{e.get('title', '')}"


class RSSSource:
    def __init__(self, cfg: RSSFeedConfig) -> None:
        self._cfg = cfg
        self.name = f"rss:{cfg.name}"
        self.interval_seconds = cfg.interval_seconds

    async def poll(self, cursor: str | None) -> AsyncIterator[tuple[NewsItemDTO, str]]:
        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True, headers={"User-Agent": _UA}
            ) as client:
                resp = await client.get(self._cfg.url)
                resp.raise_for_status()
                content = resp.content
        except Exception as e:
            log.warning("rss_fetch_failed", source=self.name, error=str(e))
            return

        parsed = feedparser.parse(content)
        cutoff = _effective_cutoff(
            cursor,
            self.interval_seconds,
            datetime.now(timezone.utc),
            settings.ingest_age_factor,
        )

        # сортируем по published ASC, чтобы курсор двигался монотонно
        entries = sorted(
            parsed.entries,
            key=lambda e: (_entry_published(e) or datetime.min.replace(tzinfo=timezone.utc)),
        )
        max_cursor = cutoff
        for e in entries:
            pub = _entry_published(e)
            if pub and pub <= cutoff:  # уже видели ИЛИ старше окна — прошлое не нужно
                continue
            dto = NewsItemDTO(
                source_type="rss",
                source_id=_source_id(e),
                url=e.get("link"),
                title=clean_title(e.get("title")),
                text=_entry_text(e),
                media=_media(e),
                author=e.get("author") or self._cfg.name,
                feed_name=self._cfg.name,
                published_at=pub,
                category=self._cfg.category,
            )
            if pub and (max_cursor is None or pub > max_cursor):
                max_cursor = pub
            new_cursor = max_cursor.isoformat() if max_cursor else (cursor or "")
            yield dto, new_cursor
