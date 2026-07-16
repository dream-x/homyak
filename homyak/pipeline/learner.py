"""Обучатель: consumer на feedback.recorded — двигает taste-вектор и веса тегов/источников.

👍/⭐ → tag/source affinity вверх + item в центроид «вкуса»; 👎 → affinity вниз (центроид не трогаем,
он = среднее лайков); 🔇 → тег в muted_tags (профиль НЕ трогаем — это декларация пользователя).
action=removed откатывает (toggle). Идемпотентность — UNIQUE(news_item_id, signal, topic) в PG.
"""

from __future__ import annotations

import asyncio
import signal as signal_mod
from contextlib import suppress

import structlog

import json

from homyak.core.config import settings
from homyak.core.events import NatsBus
from homyak.core.llm import OllamaLLM
from homyak.core.scoring import centroid_add, centroid_remove
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo
from homyak.storage.qdrant import QdrantStore

log = structlog.get_logger(__name__)

_SIGN = {"up": 1, "save": 1, "down": -1}

_REFINE_SYSTEM = (
    "Ты — куратор профиля интересов читателя новостей. По текущему профилю и его реакциям предложи "
    "уточнённый профиль. Верни строго JSON "
    '{"description": "<1 абзац на русском>", "topics": [{"name": "<тег>", "polarity": "love|like|mute"}]}. '
    "Сохрани то, что работает; в love/like — что он явно лайкает; в mute — что явно дизлайкает."
)


async def maybe_refine(repo: NewsRepo, bus: NatsBus, llm: OllamaLLM, vertical: str) -> None:
    prof = await repo.get_active_profile(vertical)
    desc = prof[1] if prof else ""
    liked, disliked = await repo.recent_liked_disliked(vertical, 14)
    if not liked and not disliked:
        return

    def fmt(items):
        return "; ".join(f"{t} [{','.join(tags)}]" for t, tags in items if t) or "—"

    user = (
        f"Вертикаль: {vertical}\nТекущий профиль: «{desc}»\n\n"
        f"Лайкал: {fmt(liked)}\n\nДизлайкал: {fmt(disliked)}\n\n"
        "Предложи уточнённый профиль (description) и 6-12 тем с polarity в рамках этой вертикали."
    )
    try:
        data = await llm.chat_json(_REFINE_SYSTEM, user)
    except Exception as e:
        log.warning("refine_failed", vertical=vertical, error=str(e))
        return
    if not isinstance(data, dict) or not data.get("description"):
        return
    topics = data.get("topics") if isinstance(data.get("topics"), list) else []
    payload = {"vertical": vertical, "description": str(data["description"]).strip(), "topics": topics}
    await repo.save_cursor(f"profile:pending:{vertical}", json.dumps(payload, ensure_ascii=False))
    await bus.publish_profile_suggestion(payload)
    log.info("profile_suggestion_published", vertical=vertical, topics=len(topics))


def make_handler(repo: NewsRepo, qdrant: QdrantStore, bus: NatsBus, llm: OllamaLLM):
    async def handle(data: dict) -> None:
        item_id = data["news_item_id"]
        sig = data["signal"]
        action = data.get("action", "added")
        lr = settings.feedback_lr

        meta = await repo.get_item_meta(item_id)
        if meta is None:
            return
        source_type, source_key, tags, vertical = meta
        if not vertical:  # статья вне вертикалей — обучать нечего
            return

        if sig == "mute_topic":
            topic = data.get("topic")
            if topic:
                # record_feedback — toggle, поэтому action="removed" (повторное 🔇) обязан снимать
                # мьют. Раньше эта ветка молча игнорировалась: отменить мьют было нельзя вообще.
                if action == "added":
                    await repo.mute_topic(vertical, topic)
                else:
                    await repo.unmute_topic(vertical, topic)
                log.info("mute_topic", vertical=vertical, topic=topic, action=action)
            return

        base = _SIGN.get(sig)
        if base is None:
            return  # open/skip — Phase 6.5
        direction = base if action == "added" else -base

        await repo.bump_tag_affinity(vertical, tags, direction, lr)
        await repo.bump_source_affinity(vertical, source_type, source_key, direction, lr)

        # taste-центроид вертикали = среднее лайкнутых; трогаем только на up/save
        if base > 0:
            vec = await qdrant.get_vector(item_id)
            if vec:
                centroid = await qdrant.get_taste(vertical)
                n = await repo.get_taste_n_liked(vertical)
                if action == "added":
                    new_c, new_n = centroid_add(centroid, n, vec)
                else:
                    new_c, new_n = centroid_remove(centroid, n, vec)
                if new_c is not None:
                    await qdrant.set_taste(vertical, new_c)
                await repo.set_taste_state(vertical, new_n)

        log.info("learned", item=item_id, vertical=vertical, signal=sig, action=action)

        # каждые N фидбеков в вертикали — предложить уточнение её профиля
        if action == "added":
            count = await repo.learnable_feedback_count(vertical)
            if count > 0 and count % settings.profile_refine_every == 0:
                await maybe_refine(repo, bus, llm, vertical)

    return handle


async def main_async() -> None:
    repo = NewsRepo(SessionFactory)
    qdrant = QdrantStore(settings.qdrant_url)
    await qdrant.ensure_taste_collection()
    bus = NatsBus(settings.nats_url)
    await bus.connect()
    llm = OllamaLLM()

    handler = make_handler(repo, qdrant, bus, llm)
    task = asyncio.create_task(bus.consume_feedback(handler, durable="learner"))
    log.info("learner_started")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal_mod.SIGINT, signal_mod.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    await stop.wait()

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await bus.close()
    await qdrant.close()
    log.info("learner_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
