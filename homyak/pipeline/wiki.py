"""Сервис homyak-wiki: строит LLM-вику из сохранённого (⭐/👍).

Consumer на `homyak.feedback.recorded` (durable `wiki`, тот же subject, что у learner) — на каждый
up/save читает запись и обновляет страницы вики (wiki_ingest). Плюс периодический lint. Отдельный
процесс/контейнер (по решению: вика — самостоятельный сервис, не часть learner'а).
"""

from __future__ import annotations

import asyncio
import signal as signal_mod
from contextlib import suppress

import structlog

from homyak.core import wiki
from homyak.core.config import settings
from homyak.core.events import NatsBus
from homyak.core.llm import OllamaLLM
from homyak.core.wiki_ingest import ingest_item
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)


def make_handler(repo: NewsRepo, llm: OllamaLLM):
    async def handle(data: dict) -> None:
        # только положительное и только добавление: toggle-off страницы не сносим (сложно/рискованно)
        if data.get("signal") not in ("up", "save") or data.get("action") != "added":
            return
        item = await repo.get_by_id(data["news_item_id"])
        if item is None or not (item.title or item.text):
            return
        with suppress(Exception):  # best-effort: одна запись не должна ронять consumer
            await ingest_item(item, llm)

    return handle


async def lint_loop(stop: asyncio.Event) -> None:
    every = settings.wiki_lint_every_hours
    if every <= 0:
        return
    while not stop.is_set():
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every * 3600)
        if stop.is_set():
            return
        with suppress(Exception):
            res = wiki.run_lint()
            log.info("wiki_linted", **res)


async def main_async() -> None:
    wiki.ensure_dirs()
    repo = NewsRepo(SessionFactory)
    bus = NatsBus(settings.nats_url)
    await bus.connect()
    llm = OllamaLLM()

    handler = make_handler(repo, llm)
    task = asyncio.create_task(bus.consume_feedback(handler, durable="wiki"))
    stop = asyncio.Event()
    lint = asyncio.create_task(lint_loop(stop))
    log.info("wiki_started", dir=settings.wiki_dir, pages=wiki.stats())

    loop = asyncio.get_running_loop()
    for s in (signal_mod.SIGINT, signal_mod.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    await stop.wait()

    task.cancel()
    lint.cancel()
    for t in (task, lint):
        with suppress(asyncio.CancelledError):
            await t
    await bus.close()
    log.info("wiki_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
