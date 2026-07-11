"""Telegram relay (PushSource): тейлит outbox JSONL, который пишет пропатченный tscrapper.

Идемпотентность гарантируется UNIQUE (source_type='telegram', source_id) в PG — даже при перечитке
outbox'а дублей не будет. Offset (в байтах) хранится в ingest_state.cursor, переживает rotation.
Патч самого tscrapper (append в outbox) — в его репозитории, здесь только приём.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from homyak.core.interfaces import NewsItemDTO
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)


class TelegramOutboxLine(BaseModel):
    source_id: str  # "chat_id:message_id"
    url: str | None = None
    title: str | None = None
    text: str = ""
    media: list[str] = Field(default_factory=list)
    author: str | None = None
    category: str | None = None
    published_at: datetime | None = None

    def to_dto(self) -> NewsItemDTO:
        return NewsItemDTO(
            source_type="telegram",
            source_id=self.source_id,
            url=self.url,
            title=self.title,
            text=self.text or None,
            media=self.media,
            author=self.author,
            category=self.category,
            published_at=self.published_at,
        )


class TelegramRelaySource:
    name = "telegram-relay"

    def __init__(self, outbox_path: str, repo: NewsRepo, poll_interval: float = 0.5) -> None:
        self._path = Path(outbox_path)
        self._repo = repo
        self._interval = poll_interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def subscribe(self, sink: Callable[[NewsItemDTO], Awaitable[None]]) -> None:
        offset = int(await self._repo.get_cursor(self.name) or 0)
        while not self._stop.is_set():
            if not self._path.exists():
                await self._wait()
                continue

            size = self._path.stat().st_size
            if size < offset:  # rotation — файл усёкся/пересоздан
                offset = 0
                await self._repo.save_cursor(self.name, "0")
            if size == offset:
                await self._wait()
                continue

            with self._path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()

            lines = chunk.split(b"\n")
            remainder = lines.pop()  # неполная последняя строка — обработаем в следующий раз
            consumed = len(chunk) - len(remainder)

            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    parsed = TelegramOutboxLine.model_validate_json(raw)
                except Exception as e:
                    log.warning("outbox_bad_line", error=str(e))
                    continue
                await sink(parsed.to_dto())

            offset += consumed
            await self._repo.save_cursor(self.name, str(offset))
            await self._wait()

    async def _wait(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        except asyncio.TimeoutError:
            pass
