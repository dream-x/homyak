"""Сервис homyak-starchan: ⭐-канал — всё, что помечено звездой, плюс дайджест дня.

Consumer на `homyak.feedback.recorded` (durable `starchan`): ловит `save`/`added` из обеих
поверхностей сразу — и из бота, и из `/lenta`, потому что фидбек публикуется в NATS обеими.

Почему отдельный процесс, а не внутри tgbot: карточка требует LLM-вызова и, если текста мало,
похода в сеть за статьёй — это секунды, и им нечего делать в цикле пушей. Прецедент — homyak-wiki.

Второй инстанс Bot тут безопасен: конфликтует только getUpdates (его ведёт tgbot), отправка —
обычный HTTP-вызов.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import signal as signal_mod
from datetime import datetime, timezone

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from nats.js.api import DeliverPolicy

from homyak.core.article import fetch_article
from homyak.core.config import settings
from homyak.core.digest import build_digest
from homyak.core.events import NatsBus
from homyak.core.llm import OllamaLLM
from homyak.core.starcard import MIN_FULL, Card, make_card, topic_emoji
from homyak.core.textutils import fmt_age, fmt_when, hashtags
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)

DIGEST_KEY = "starchan:digest"  # {"date": "2026-08-13", "hours": [10], "seen": [id, ...]}
_TG_SAFE = 3900
_SEEN_KEEP = 60  # сколько id помнить, чтобы вечерний дайджест не повторял утренний


def _esc(s: str | None) -> str:
    return html.escape(s or "")


def _target(chan: str) -> int | str:
    return int(chan) if chan.lstrip("-").isdigit() else chan  # -100… либо @username


def _src_label(item) -> str:
    feed = getattr(item, "feed_name", None) or ""
    if feed.startswith("tw_"):
        return "🐦 @" + feed[3:]
    if feed.startswith("gh_"):
        return "🐙 GitHub"
    if feed.startswith("@"):
        return "✈️ " + feed
    return "📡 " + (feed or item.source_type or "источник")


def _when(item) -> str:
    """«14:32 · 3ч» — фактическое время новости плюс возраст (как в карточках бота)."""
    ts = getattr(item, "published_at", None) or getattr(item, "fetched_at", None)
    if ts is None:
        return ""
    t = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts  # naive из БД = UTC
    age = fmt_age((datetime.now(timezone.utc) - t).total_seconds())
    return " · ".join(x for x in (fmt_when(ts), age) if x)


def render_card(item, card: Card) -> str:
    """Карточка для канала. Заголовок — оригинальный (по решению владельца) и кликабельный.

    Значок — по теме записи, а не общая звезда: лента из одинаковых ⭐ не читается взглядом,
    а тема видна до чтения заголовка.
    """
    emoji = topic_emoji(getattr(item, "tags", None), getattr(item, "feed_name", None))
    title = _esc(item.title or "(без заголовка)")
    head = (
        f'{emoji} <a href="{_esc(item.url)}"><b>{title}</b></a>'
        if item.url
        else f"{emoji} <b>{title}</b>"
    )
    parts = [head]
    if card.line:
        parts.append(f"\n{_esc(card.line)}")
    if card.points:
        parts.append("\n" + "\n".join(f"• {_esc(p)}" for p in card.points))
    foot = _src_label(item)
    when = _when(item)
    if when:
        foot += f" · 🕒 {when}"
    tags = hashtags(getattr(item, "tags", None))
    parts.append("\n" + foot + (f"\n{tags}" if tags else ""))
    card_text = "\n".join(parts)
    if len(card_text) > _TG_SAFE:
        card_text = card_text[: _TG_SAFE - 1] + "…"
    return card_text


async def ensure_text(item, repo: NewsRepo) -> str:
    """Текста мало → пробуем дотянуть статью ещё раз (при ингесте могло не выйти).

    Треть звёзд приходит с lobsters/hn/github, где в базе 0-130 символов; без этой попытки
    большинство ⭐-карточек выходило бы голыми. Удачу сохраняем в БД — от полного текста
    выигрывают и поиск, и вика, и «Разбор» в боте.
    """
    text = (item.text or "").strip()
    if len(text) >= MIN_FULL or not item.url:
        return text
    fresh = None
    with contextlib.suppress(Exception):  # best-effort: сеть не должна ронять публикацию
        fresh = await fetch_article(item.url)
    if fresh and len(fresh) > len(text):
        with contextlib.suppress(Exception):
            await repo.set_item_text(item.id, fresh)
        log.info("starchan_text_refetched", item=item.id, chars=len(fresh))
        return fresh
    return text


async def publish_star(item_id: int, repo: NewsRepo, bot: Bot, llm: OllamaLLM) -> bool:
    if not settings.star_channel_id:
        return False
    item = await repo.get_by_id(item_id)
    if item is None or item.star_posted_at is not None:  # повторная звезда → без дубля
        return False
    # Фильтр вертикали — про АВТОМАТИЧЕСКИЕ звёзды: он держит канал в IT-теме. Ссылку,
    # принесённую человеком через бота, он резать не должен: её тему выбрал человек, и
    # молча не опубликовать её — худшее из возможных поведений.
    want = settings.star_vertical.strip()
    if want and item.source_type != "manual" and (item.vertical or "") != want:
        log.info("starchan_skipped_vertical", item=item_id, vertical=item.vertical)
        return False
    text = await ensure_text(item, repo)
    card = await make_card(item.title, text, llm)
    if card.mode == "failed":
        # Текст есть, а модель не ответила — откладываем на ретрай консюмера. Публиковать
        # сейчас значило бы отдать голый заголовок по статье, которая у нас уже скачана.
        raise RuntimeError(f"LLM недоступна, звезда {item_id} ждёт ретрая")
    if card.dropped:
        log.warning("starchan_ungrounded", item=item_id, dropped=card.dropped)
    await bot.send_message(
        _target(settings.star_channel_id),
        render_card(item, card),
        disable_web_page_preview=False,
    )
    await repo.mark_star_posted(item.id)
    log.info("starchan_posted", item=item.id, mode=card.mode, dropped=len(card.dropped))
    return True


def make_handler(repo: NewsRepo, bot: Bot, llm: OllamaLLM):
    async def handle(data: dict) -> None:
        if data.get("signal") != "save" or data.get("action") != "added":
            return
        # Снятие звезды пост НЕ удаляет (решение владельца): у подписчиков он бы исчез без следа.
        #
        # Исключение НЕ глушим: сбой отправки в Telegram (падал прокси — 6-8.08 на двое суток)
        # должен уйти в nak+backoff консюмера и доехать позже, а не быть съеденным вместе с
        # ack'ом. Ошибки LLM и сети за статьёй сюда не доходят — их гасит сам make_card.
        await publish_star(data["news_item_id"], repo, bot, llm)

    return handle


# --- дайджест дня ---


def parse_hours(raw: str) -> list[int]:
    out = []
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            h = int(part)
        except ValueError:
            log.warning("starchan_bad_digest_hour", value=part)
            continue
        if 0 <= h <= 23:
            out.append(h)
    return sorted(set(out))


def due_hour(now: datetime, hours: list[int], state: dict) -> int | None:
    """Какой слот дайджеста пора слать (или None).

    Считаем по календарной дате, а не по «прошло N часов»: перезапуск сервиса не должен
    порождать лишний дайджест, а пропущенный из-за простоя слот того же дня — досылается.
    """
    today = now.date().isoformat()
    done = set(state.get("hours", [])) if state.get("date") == today else set()
    for h in hours:
        if now.hour >= h and h not in done:
            return h
    return None


async def _read_state(repo: NewsRepo) -> dict:
    raw = await repo.get_cursor(DIGEST_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _digest_text(res: dict, when: str) -> str:
    parts = [f"📰 <b>Дайджест · {when}</b> — топ-{res['n']} за сутки\n"]
    if res.get("intro"):
        parts.append(_esc(res["intro"]) + "\n")
    for i, it in enumerate(res["items"], 1):
        title = _esc(it.get("title") or "—")
        url = it.get("url")
        head = f'<a href="{_esc(url)}">{title}</a>' if url else f"<b>{title}</b>"
        emoji = topic_emoji(it.get("tags"), it.get("feed"))
        parts.append(f"{i}. {emoji} {head}")
        desc = (it.get("summary") or "").replace("\n", " ").strip()
        if desc:
            if len(desc) > 140:
                desc = desc[:140].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
            parts.append(f"    {_esc(desc)}")
        parts.append("")
    return "\n".join(parts).rstrip()


async def send_digest(repo: NewsRepo, bot: Bot, llm: OllamaLLM, hour: int) -> bool:
    """Топ-N ленты за сутки в канал. Пул — вся IT-лента, а не только звёзды: канал должен
    жить и в дни без звёзд (их бывает 0-1)."""
    state = await _read_state(repo)
    today = datetime.now().date().isoformat()
    seen = state.get("seen", []) if state.get("date") == today else []
    res = await build_digest(
        24,
        limit=settings.star_digest_limit,
        llm=llm,
        exclude=seen,
        vertical=settings.star_vertical.strip() or None,
    )
    if not res["n"]:
        log.info("starchan_digest_empty", hour=hour)
        return False
    await bot.send_message(
        _target(settings.star_channel_id),
        _digest_text(res, f"{hour}:00"),
        disable_web_page_preview=True,
    )
    done = set(state.get("hours", [])) if state.get("date") == today else set()
    await repo.save_cursor(
        DIGEST_KEY,
        json.dumps(
            {
                "date": today,
                "hours": sorted(done | {hour}),
                "seen": ([it["id"] for it in res["items"]] + seen)[:_SEEN_KEEP],
            }
        ),
    )
    log.info("starchan_digest_sent", hour=hour, n=res["n"])
    return True


async def digest_loop(repo: NewsRepo, bot: Bot, llm: OllamaLLM, stop: asyncio.Event) -> None:
    hours = parse_hours(settings.star_digest_hours)
    if not hours or not settings.star_channel_id:
        log.info("starchan_digest_off")
        return
    log.info("starchan_digest_scheduled", hours=hours)
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=300)
        if stop.is_set():
            return
        with contextlib.suppress(Exception):
            state = await _read_state(repo)
            hour = due_hour(datetime.now(), hours, state)
            if hour is not None:
                await send_digest(repo, bot, llm, hour)


async def main_async() -> None:
    if not settings.star_channel_id:
        log.warning("starchan_disabled", reason="STAR_CHANNEL_ID пуст")
    repo = NewsRepo(SessionFactory)
    session = AiohttpSession(proxy=settings.telegram_bot_proxy) if settings.telegram_bot_proxy else None
    bot = Bot(
        settings.telegram_bot_token or "",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    llm = OllamaLLM(model=settings.summary_model, fallback=settings.summary_fallback_model)
    bus = NatsBus(settings.nats_url)
    await bus.connect()

    stop = asyncio.Event()
    # DeliverPolicy.NEW: канал начинается с чистого листа. По умолчанию новый durable
    # проиграл бы весь стрим фидбека — это залп сотни старых ⭐ подписчикам.
    consumer = asyncio.create_task(
        bus.consume_feedback(
            make_handler(repo, bot, llm), durable="starchan", deliver_policy=DeliverPolicy.NEW
        )
    )
    digest = asyncio.create_task(digest_loop(repo, bot, llm, stop))
    log.info(
        "starchan_started",
        channel=settings.star_channel_id or "off",
        vertical=settings.star_vertical or "any",
    )

    loop = asyncio.get_running_loop()
    for s in (signal_mod.SIGINT, signal_mod.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    await stop.wait()

    consumer.cancel()
    digest.cancel()
    for t in (consumer, digest):
        with contextlib.suppress(asyncio.CancelledError):
            await t
    await bus.close()
    await bot.session.close()
    log.info("starchan_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
