"""Miniflux source-адаптер (PollSource).

Дёргаем REST напрямую через httpx (async), а не синхронный `miniflux` SDK — чище для пайплайна
и тестируется через respx. Курсор — max entry.id, endpoint `after_entry_id`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import httpx
import structlog

from homyak.core.config import MinifluxConfig
from homyak.core.interfaces import NewsItemDTO

log = structlog.get_logger(__name__)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class MinifluxSource:
    def __init__(self, cfg: MinifluxConfig) -> None:
        self._cfg = cfg
        self.name = "miniflux"
        self.interval_seconds = cfg.interval_seconds

    async def poll(self, cursor: str | None) -> AsyncIterator[tuple[NewsItemDTO, str]]:
        after = int(cursor) if cursor and cursor.isdigit() else 0
        params = {
            "after_entry_id": after,
            "limit": 100,
            "order": "id",
            "direction": "asc",
        }
        headers = {"X-Auth-Token": self._cfg.token or ""}
        try:
            async with httpx.AsyncClient(
                base_url=self._cfg.base_url, timeout=30, headers=headers
            ) as client:
                resp = await client.get("/v1/entries", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            log.warning("miniflux_fetch_failed", error=str(e))
            return

        allowed = set(self._cfg.categories or [])
        max_id = after
        for entry in data.get("entries", []):
            feed = entry.get("feed") or {}
            cat_title = ((feed.get("category") or {}).get("title") or "").lower()
            if allowed and cat_title not in allowed:
                # всё равно двигаем курсор, чтобы не перечитывать отфильтрованное
                max_id = max(max_id, int(entry["id"]))
                continue
            dto = NewsItemDTO(
                source_type="miniflux",
                source_id=str(entry["id"]),
                url=entry.get("url"),
                title=entry.get("title"),
                text=entry.get("content"),
                author=entry.get("author") or feed.get("title"),
                published_at=_parse_iso(entry.get("published_at")),
                category=cat_title or None,
            )
            max_id = max(max_id, int(entry["id"]))
            yield dto, str(max_id)
