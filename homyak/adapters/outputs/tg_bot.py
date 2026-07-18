"""Telegram-бот: персональный push + кнопки-фидбек 👍/👎/⭐/🔇 + команды.

Push: подписан на JetStream items.processed, шлёт item'ы с personal_score>=порога (rate-limit,
тихие часы, pushed_at). Кнопки → record_feedback + publish feedback.recorded (обучает learner).
"""

from __future__ import annotations

import asyncio
import html
import json
import time
from contextlib import suppress
from datetime import datetime

import structlog
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from nats.js.api import ConsumerConfig, DeliverPolicy

from homyak.core import telegraph
from homyak.core.article import fetch_article
from homyak.core.config import settings
from homyak.core.events import SUBJECT_PROCESSED, NatsBus
from homyak.core.interests import weights as interest_weights
from homyak.core.interfaces import FeedQuery
from homyak.core.razbor import build_razbor
from homyak.core.scoring import freshness, weights_from_interests
from homyak.core.textutils import hashtags, strip_html
from homyak.core.verticals import LABELS, VERTICALS
from homyak.storage.db import SessionFactory
from homyak.storage.postgres import NewsRepo

log = structlog.get_logger(__name__)

CHAT_KEY = "tgbot:chat_id"
THRESHOLD_KEY = "tgbot:threshold"
PAUSED_KEY = "tgbot:paused_until"
PUSH_VERTICALS_KEY = "tgbot:push_verticals"  # csv разрешённых вертикалей для пуша; пусто = все

# алиасы для /pushonly (ru/en/сокращения) → каноническая вертикаль
_V_ALIAS = {
    "business": "business", "biz": "business", "бизнес": "business", "б": "business",
    "it": "it", "ит": "it", "tech": "it", "айти": "it",
    "medical": "medical", "med": "medical", "мед": "medical", "медикал": "medical",
}

_repo = NewsRepo(SessionFactory)
_bus: NatsBus | None = None
_bot: Bot | None = None
dp = Dispatcher()

# Кнопки постоянной клавиатуры — 3 вертикали + сервис
BTN_BIZ = "💼 Business"
BTN_IT = "💻 IT"
BTN_MED = "🩺 Medical"
BTN_WATCH = "👁 Watchlist"
BTN_INSIGHTS = "💡 Insights"
BTN_TWITTER = "🐦 Twitter"
BTN_DIGEST = "📰 Дайджест"
BTN_PUSH = "🔔 Пуши"
BTN_SOURCES = "📡 Источники"
BTN_PROFILE = "👤 Профили"
BTN_STATS = "📊 Статистика"
_BTN_VERTICAL = {BTN_BIZ: "business", BTN_IT: "it", BTN_MED: "medical"}

# Ряд 1 — переключение вертикали (лента + пуши следуют за ней); ряд 2/3 — срезы/сервис.
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_BIZ), KeyboardButton(text=BTN_IT), KeyboardButton(text=BTN_MED)],
        [KeyboardButton(text=BTN_WATCH), KeyboardButton(text=BTN_TWITTER), KeyboardButton(text=BTN_DIGEST)],
        [KeyboardButton(text=BTN_PUSH), KeyboardButton(text=BTN_SOURCES), KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_STATS)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Меню команд (кнопка «/» в клиенте Telegram)
BOT_COMMANDS = [
    BotCommand(command="business", description="💼 Лента: бизнес/рынки"),
    BotCommand(command="it", description="💻 Лента: технологии/IT"),
    BotCommand(command="medical", description="🩺 Лента: медицина"),
    BotCommand(command="trending", description="👁 Watchlist: трендовые темы под вниманием"),
    BotCommand(command="digest", description="📰 Топ по всем вертикалям"),
    BotCommand(command="twitter", description="🐦 Только твиттер (все аккаунты)"),
    BotCommand(command="profile", description="👤 Мои профили (3 вертикали)"),
    BotCommand(command="stats", description="📊 Статистика обучения"),
    BotCommand(command="why", description="🔍 Разбор скоринга: /why <id>"),
    BotCommand(command="sources", description="📡 Источники в ленте"),
    BotCommand(command="mute", description="🔇 Замьютить тему: /mute <тема>"),
    BotCommand(command="pushonly", description="🔔 Пуши только: /pushonly it [business ...]"),
    BotCommand(command="pushall", description="🌐 Пуши по всем вертикалям"),
    BotCommand(command="threshold", description="🎚 Порог пуша: /threshold <0..1>"),
    BotCommand(command="pause", description="⏸ Пауза пушей: /pause [часы]"),
    BotCommand(command="start", description="🚀 Запустить/перезапустить"),
]


# --- форматирование ---


def _esc(s: str | None) -> str:
    return html.escape(s or "")


_TG_LIMIT = 4096
_TG_SAFE = 3900  # запас на служебную разметку; summary ничем не ограничен (Text-колонка)


def _fmt(item) -> str:
    pct = int(round((item.personal_score or 0) * 100))
    vlabel = LABELS.get(item.vertical, "")
    watch = getattr(item, "watch_topics", None) or []
    watch_badge = f"👁 <b>{_esc(', '.join(watch))}</b>\n" if watch else ""
    head = f"{watch_badge}{vlabel + '  ' if vlabel else ''}🎯 <b>{pct}%</b>"

    # Заголовок — кликабельная ссылка на оригинал: прочитал саммари → сразу в источник.
    title = _esc(item.title or "(без заголовка)")
    body = f'<a href="{_esc(item.url)}"><b>{title}</b></a>' if item.url else f"<b>{title}</b>"
    if item.summary:
        body += f"\n{_esc(item.summary)}"

    feed = getattr(item, "feed_name", None) or ""
    if feed.startswith("tw_"):  # твиттер через RSSHub — помечаем 🐦 и хэндлом
        src = f"🐦 <b>@{_esc(feed[3:])}</b>"
    else:
        src = _esc(item.source_type + (f"/{item.author}" if item.author else ""))
    tags = hashtags(item.tags)  # кликабельные #хэштеги — по ним ищется весь чат
    foot = src + (f"\n{tags}" if tags else "")
    card = f"{head}\n\n{body}\n\n{foot}"
    if len(card) > _TG_SAFE:
        # Обрезаем САММАРИ, а не хвост: иначе потеряем ссылку и теги. Без этого карточка
        # >4096 роняла отправку — в лентах рвался цикл, в пушах айтем терялся молча
        # (suppress(Exception) в push_loop → mark_pushed не вызывался).
        overflow = len(card) - _TG_SAFE + 1
        summary = _esc(item.summary or "")
        if len(summary) > overflow:
            body = body[: len(body) - overflow - 1] + "…"
            card = f"{head}\n\n{body}\n\n{foot}"
        else:  # саммари не спасает — режем всё целиком
            card = card[: _TG_SAFE - 1] + "…"
    return card


def _kb(item_id: int, url: str | None) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton(text="👍", callback_data=f"fb:up:{item_id}"),
        InlineKeyboardButton(text="👎", callback_data=f"fb:down:{item_id}"),
        InlineKeyboardButton(text="⭐", callback_data=f"fb:save:{item_id}"),
    ]
    row2 = [
        InlineKeyboardButton(text="📝 Разбор", callback_data=f"rz:{item_id}"),
        InlineKeyboardButton(text="📄 текст", callback_data=f"txt:{item_id}"),
    ]
    row3 = [InlineKeyboardButton(text="🔇", callback_data=f"fb:mute:{item_id}")]
    if url:
        row3.append(InlineKeyboardButton(text="🔗 Источник", url=url))
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])


def _chunks(s: str, n: int = 4000):
    """Бьёт длинный текст на куски <= n, по возможности по переносу строки/пробелу."""
    while s:
        if len(s) <= n:
            yield s
            return
        cut = s.rfind("\n", 0, n)
        if cut < n // 2:
            cut = s.rfind(" ", 0, n)
        if cut < n // 2:
            cut = n
        yield s[:cut]
        s = s[cut:].lstrip()


def _in_quiet(hour: int, quiet: str) -> bool:
    try:
        a, b = (int(x) for x in quiet.split("-"))
    except Exception:
        return False
    if a == b:
        return False
    return a <= hour < b if a < b else (hour >= a or hour < b)


# --- команды ---


@dp.message(Command("start"))
async def cmd_start(m: Message) -> None:
    await _repo.save_cursor(CHAT_KEY, str(m.chat.id))
    await m.answer(
        "Homyak на связи 🐹\nТри тематические ленты — 💼 Business, 💻 IT, 🩺 Medical — каждая учится "
        "отдельно на твоих 👍/👎.\n\nЖми кнопки внизу или команды /business /it /medical.",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("menu"))
async def cmd_menu(m: Message) -> None:
    await m.answer("Клавиатура:", reply_markup=MAIN_KB)


async def _send_digest(m: Message, n: int = 10) -> None:
    items = await _repo.digest(min(n, 20))
    if not items:
        await m.answer("Пусто — нет новых персональных новостей.")
        return
    for it in items:
        await m.answer(_fmt(it), reply_markup=_kb(it.id, it.url))
        await _repo.mark_pushed(it.id)


@dp.message(Command("digest"))
async def cmd_digest(m: Message, command: CommandObject) -> None:
    n = int(command.args) if command.args and command.args.strip().isdigit() else 10
    await _send_digest(m, n)


async def _send_vertical(m: Message, vertical: str, n: int = 8) -> None:
    # Переключение вертикали: активная лента = пуши. Жмёшь 💻 IT → и лента IT, и пуши IT.
    await _repo.save_cursor(PUSH_VERTICALS_KEY, vertical)
    lbl = LABELS[vertical]
    result = await _repo.feed(FeedQuery(sort="personal", vertical=vertical, limit=n))
    if not result.items:
        await m.answer(f"{lbl}: пока пусто — копится. 🔔 Пуши переключены на {lbl}.")
        return
    await m.answer(f"{lbl} — топ под твой профиль. 🔔 Пуши теперь только {lbl} (🔔 → сменить):")
    for it in result.items:
        await m.answer(_fmt(it), reply_markup=_kb(it.id, it.url))
        await _repo.mark_pushed(it.id)


async def _send_watch(m: Message, n: int = 12) -> None:
    # Кросс-вертикальный срез: айтемы, попавшие в трендовые темы вотчлиста.
    result = await _repo.feed(FeedQuery(sort="personal", has_watch=True, limit=n))
    if not result.items:
        await m.answer("👁 Watchlist: пока пусто — как появятся посты по темам, соберу.")
        return
    await m.answer("👁 Watchlist — трендовые темы под пристальным вниманием:")
    for it in result.items:
        await m.answer(_fmt(it), reply_markup=_kb(it.id, it.url))
        await _repo.mark_pushed(it.id)


@dp.message(Command("trending"))
async def cmd_trending(m: Message) -> None:
    await _send_watch(m, 12)


INSIGHT_MIN = 0.5  # порог insight_score для ленты 💡 Insights (лента всё равно сортирует по убыванию)


async def _send_insights(m: Message, n: int = 10) -> None:
    # Кросс-вертикальный срез: посты с реальной мыслью (insight_score высок), приоритет людям.
    result = await _repo.feed(FeedQuery(sort="insight", min_insight=INSIGHT_MIN, limit=n))
    if not result.items:
        await m.answer("💡 Insights: пока пусто — копятся (детектор ставит insight свежим постам).")
        return
    await m.answer("💡 Insights — посты с реальной мыслью (все источники):")
    for it in result.items:
        await m.answer(_fmt(it), reply_markup=_kb(it.id, it.url))
        await _repo.mark_pushed(it.id)


# /insights и кнопка 💡 скрыты: insight_score у текущей модели жмётся в 0.2-0.4,
# порог 0.5 → лента почти всегда пустая и непонятная. Расчёт в БД продолжается.
# @dp.message(Command("insights"))
# async def cmd_insights(m: Message) -> None:
#     await _send_insights(m, 10)


async def _send_twitter(m: Message, n: int = 15) -> None:
    # Таймлайн по всем твиттер-аккаунтам (feed_name tw_*): по СВЕЖЕСТИ, без привязки к вертикали.
    # sort=personal раньше топил 45% твитов без вертикали (personal_score=NULL) — их не было
    # видно вообще. Здесь Twitter — отдельная история, а не гонка за слоты вертикальных пушей.
    result = await _repo.feed(FeedQuery(sort="recent", feed_prefix="tw_", limit=n))
    if not result.items:
        await m.answer("🐦 Twitter: пока пусто — твиты копятся.")
        return
    await m.answer("🐦 Twitter — свежее по всем аккаунтам:")
    for it in result.items:
        await m.answer(_fmt(it), reply_markup=_kb(it.id, it.url))
        await _repo.mark_pushed(it.id)


@dp.message(Command("twitter"))
async def cmd_twitter(m: Message) -> None:
    await _send_twitter(m, 10)


@dp.message(Command("business"))
async def cmd_business(m: Message) -> None:
    await _send_vertical(m, "business")


@dp.message(Command("it"))
async def cmd_it(m: Message) -> None:
    await _send_vertical(m, "it")


@dp.message(Command("medical"))
async def cmd_medical(m: Message) -> None:
    await _send_vertical(m, "medical")


@dp.message(Command("profile"))
async def cmd_profile(m: Message) -> None:
    blocks = []
    for v in VERTICALS:
        prof = await _repo.get_active_profile(v)
        if prof is None:
            blocks.append(f"<b>{LABELS[v]}</b>: профиль не задан")
            continue
        ver, desc, topics = prof
        mutes = [t["name"] for t in topics if t.get("polarity") == "mute"]
        blocks.append(
            f"<b>{LABELS[v]} v{ver}</b>\n{_esc(desc[:220])}"
            + (f"\n🔇 {_esc(', '.join(mutes))}" if mutes else "")
        )
    await m.answer("\n\n".join(blocks))


@dp.message(Command("stats"))
async def cmd_stats(m: Message) -> None:
    counts = await _repo.feedback_counts()
    up = counts.get("up", 0) + counts.get("save", 0)
    down = counts.get("down", 0)
    total = up + down
    prec = f"{100 * up / total:.0f}%" if total else "—"
    # await нельзя внутри генератора для str.join (получится async-генератор) — собираем циклом
    taste_parts = [f"{LABELS[v]} {await _repo.get_taste_n_liked(v)}" for v in VERTICALS]
    tastes = ", ".join(taste_parts)
    await m.answer(
        f"👍 {up}  👎 {down}  🔇 {counts.get('mute_topic', 0)}\n"
        f"precision (доля 👍): {prec}\nвектор вкуса (лайков): {tastes}"
    )


@dp.message(Command("why"))
async def cmd_why(m: Message, command: CommandObject) -> None:
    if not command.args or not command.args.strip().isdigit():
        await m.answer("Использование: /why &lt;id&gt;")
        return
    item = await _repo.get_by_id(int(command.args.strip()))
    if item is None:
        await m.answer("Нет такого item.")
        return
    vertical = item.vertical or "it"
    tags = list(item.tags or [])
    tag_affs = await _repo.get_tag_affinities(vertical, tags)
    tag_aff = sum(tag_affs.values()) / len(tag_affs) if tag_affs else 0.0
    src_aff = await _repo.get_source_affinity(vertical, item.source_type, item.feed_name or item.author)
    fr = freshness(item.published_at)
    w = weights_from_interests()
    llm = item.llm_relevance if item.llm_relevance is not None else 0.0
    ps = "— (mute)" if item.personal_score is None else f"{item.personal_score:.3f}"
    parts = (
        f"llm {w.llm}×{llm:.2f} + taste(ramp) + "
        f"tag {w.tag}×{tag_aff:.2f} + src {w.source}×{src_aff:.2f} + fresh {w.fresh}×{fr:.2f}"
    )
    reason = f"\n💡 {_esc(item.llm_reason)}" if item.llm_reason else ""
    await m.answer(f"<b>{_esc(item.title or '')}</b>\npersonal_score = {ps}\n{parts}{reason}")


@dp.message(Command("threshold"))
async def cmd_threshold(m: Message, command: CommandObject) -> None:
    if not command.args:
        cur = await _repo.get_cursor(THRESHOLD_KEY)
        await m.answer(f"Порог пуша: {cur or interest_weights().push_threshold}")
        return
    try:
        v = float(command.args.strip())
    except ValueError:
        await m.answer("Нужно число 0..1")
        return
    await _repo.save_cursor(THRESHOLD_KEY, str(v))
    await m.answer(f"Порог пуша = {v}")


@dp.message(Command("pause"))
async def cmd_pause(m: Message, command: CommandObject) -> None:
    hours = int(command.args) if command.args and command.args.strip().isdigit() else 8
    await _repo.save_cursor(PAUSED_KEY, str(time.time() + hours * 3600))
    await m.answer(f"⏸ Пауза пушей на {hours}ч.")


@dp.message(Command("mute"))
async def cmd_mute(m: Message, command: CommandObject) -> None:
    if not command.args:
        await m.answer("Использование: /mute &lt;тема&gt;")
        return
    topic = command.args.strip().lower()
    for v in VERTICALS:
        await _repo.mute_topic(v, topic)
    await m.answer(f"🔇 «{_esc(topic)}» замьючено во всех вертикалях.")


@dp.message(Command("sources"))
async def cmd_sources(m: Message) -> None:
    counts = await _repo.feed_source_counts(30)
    if not counts:
        await m.answer("Источников в ленте пока нет.")
        return
    lines = "\n".join(f"• <code>{_esc(name)}</code> — {n}" for name, n in counts)
    await m.answer(
        f"<b>Источники в ленте</b> (фид — items):\n{lines}\n\nФильтр: /source &lt;имя&gt;"
    )


@dp.message(Command("source"))
async def cmd_source(m: Message, command: CommandObject) -> None:
    if not command.args:
        await m.answer("Использование: /source &lt;имя фида&gt; · список: /sources")
        return
    name = command.args.strip()
    result = await _repo.feed(FeedQuery(sort="personal", feed_name=name, limit=8))
    if not result.items:
        await m.answer(f"Нет персональных новостей из «{_esc(name)}». Проверь имя: /sources")
        return
    await m.answer(f"📡 Топ из «{_esc(name)}»:")
    for it in result.items:
        await m.answer(_fmt(it), reply_markup=_kb(it.id, it.url))


# --- кнопки постоянной клавиатуры ---


@dp.message(F.text.in_(list(_BTN_VERTICAL)))
async def btn_vertical(m: Message) -> None:
    await _send_vertical(m, _BTN_VERTICAL[m.text])


@dp.message(F.text == BTN_PROFILE)
async def btn_profile(m: Message) -> None:
    await cmd_profile(m)


@dp.message(F.text == BTN_STATS)
async def btn_stats(m: Message) -> None:
    await cmd_stats(m)


@dp.message(F.text == BTN_WATCH)
async def btn_watch(m: Message) -> None:
    await _send_watch(m, 12)


# @dp.message(F.text == BTN_INSIGHTS)
# async def btn_insights(m: Message) -> None:
#     await _send_insights(m, 10)


@dp.message(F.text == BTN_TWITTER)
async def btn_twitter(m: Message) -> None:
    await _send_twitter(m, 10)


@dp.message(F.text == BTN_DIGEST)
async def btn_digest(m: Message) -> None:
    await _send_digest(m, 10)


@dp.message(F.text == BTN_SOURCES)
async def btn_sources(m: Message) -> None:
    await cmd_sources(m)


# --- управление пушами: кнопка 🔔 Пуши + инлайн (скоуп/порог/пауза) ---


def _push_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💼 Business", callback_data="push:only:business"),
                InlineKeyboardButton(text="💻 IT", callback_data="push:only:it"),
                InlineKeyboardButton(text="🩺 Medical", callback_data="push:only:medical"),
            ],
            [InlineKeyboardButton(text="🌐 Все вертикали", callback_data="push:all")],
            [
                InlineKeyboardButton(text="🎚 Порог ➖", callback_data="push:thr:down"),
                InlineKeyboardButton(text="➕", callback_data="push:thr:up"),
                InlineKeyboardButton(text="↺", callback_data="push:thr:reset"),
            ],
            [
                InlineKeyboardButton(text="⏸ Пауза 8ч", callback_data="push:pause"),
                InlineKeyboardButton(text="▶️ Снять паузу", callback_data="push:resume"),
            ],
        ]
    )


async def _push_scope_text() -> str:
    cur = await _repo.get_cursor(PUSH_VERTICALS_KEY)
    scope = ", ".join(LABELS.get(v, v) for v in cur.split(",")) if cur else "все вертикали 🌐"
    thr = await _repo.get_cursor(THRESHOLD_KEY)
    threshold = thr or interest_weights().push_threshold
    pnote = ""
    paused = await _repo.get_cursor(PAUSED_KEY)
    if paused and time.time() < float(paused):
        pnote = f"\n⏸ Пауза ещё ~{int((float(paused) - time.time()) / 60)} мин."
    return f"🔔 <b>Пуши</b>: {scope}\nПорог: {threshold}{pnote}\n\nЧто присылать?"


@dp.message(F.text == BTN_PUSH)
async def btn_push(m: Message) -> None:
    await m.answer(await _push_scope_text(), reply_markup=_push_kb())


@dp.callback_query(F.data.startswith("push:"))
async def on_push(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    action = parts[1]
    if action == "all":
        await _repo.save_cursor(PUSH_VERTICALS_KEY, "")
        await cq.answer("Пуши: все вертикали")
    elif action == "only" and len(parts) > 2 and parts[2] in VERTICALS:
        await _repo.save_cursor(PUSH_VERTICALS_KEY, parts[2])
        await cq.answer(f"Пуши: только {LABELS.get(parts[2], parts[2])}")
    elif action == "pause":
        await _repo.save_cursor(PAUSED_KEY, str(time.time() + 8 * 3600))
        await cq.answer("⏸ Пауза 8ч")
    elif action == "resume":
        await _repo.save_cursor(PAUSED_KEY, "0")
        await cq.answer("▶️ Пауза снята")
    elif action == "thr":
        cur = await _repo.get_cursor(THRESHOLD_KEY)
        val = float(cur) if cur else interest_weights().push_threshold
        op = parts[2] if len(parts) > 2 else "reset"
        val = interest_weights().push_threshold if op == "reset" else val + (0.05 if op == "up" else -0.05)
        val = max(0.0, min(1.0, round(val, 2)))
        await _repo.save_cursor(THRESHOLD_KEY, str(val))
        await cq.answer(f"Порог: {val}")
    else:
        await cq.answer("?")
        return
    with suppress(Exception):
        await cq.message.edit_text(await _push_scope_text(), reply_markup=_push_kb())


@dp.message(Command("pushonly"))
async def cmd_pushonly(m: Message, command: CommandObject) -> None:
    if not command.args:
        await m.answer(await _push_scope_text(), reply_markup=_push_kb())
        return
    vs: list[str] = []
    for t in command.args.lower().replace(",", " ").split():
        v = _V_ALIAS.get(t)
        if v and v not in vs:
            vs.append(v)
    if not vs:
        await m.answer("Пример: /pushonly it · /pushonly it business · /pushall — вернуть все")
        return
    await _repo.save_cursor(PUSH_VERTICALS_KEY, ",".join(vs))
    await m.answer(f"🔔 Пуши теперь только: {', '.join(LABELS.get(v, v) for v in vs)}")


@dp.message(Command("pushall"))
async def cmd_pushall(m: Message) -> None:
    await _repo.save_cursor(PUSH_VERTICALS_KEY, "")
    await m.answer("🔔 Пуши по всем вертикалям.")


# --- кнопки-фидбек ---


@dp.callback_query(F.data == "noop")
async def on_noop(cq: CallbackQuery) -> None:
    await cq.answer("уже учтено")


async def _article_text(item, item_id: int) -> str:
    """Полный текст статьи: чистим item.text, а если огрызок — качаем по URL и сохраняем."""
    text = strip_html(item.text) or ""  # защитно чистим HTML/junk («Comments»)
    if len(text) < 400 and item.url and item.source_type != "telegram":
        fetched = await fetch_article(item.url)
        if fetched and len(fetched) > len(text):
            text = fetched
            await _repo.set_item_text(item_id, fetched)
    return text


async def _telegraph_page(title: str | None, text: str, author, url) -> str | None:
    """Публикует текст как telegra.ph-страницу (Instant View), переиспользуя аккаунт-токен."""
    token = await _repo.get_cursor("telegraph:token")
    if not token:
        token = await telegraph.create_account()
        if token:
            await _repo.save_cursor("telegraph:token", token)
    return await telegraph.create_page(token, title, text, author, url) if token else None


@dp.callback_query(F.data.startswith("txt:"))
async def on_text(cq: CallbackQuery) -> None:
    try:
        item_id = int(cq.data.split(":")[1])
    except (ValueError, IndexError):
        await cq.answer("bad")
        return
    item = await _repo.get_by_id(item_id)
    if item is None:
        await cq.answer("нет такой статьи")
        return

    await cq.answer("Открываю…")
    text = await _article_text(item, item_id)
    if not text:
        await cq.message.answer("Полного текста нет — жми 🔗 (ссылка на оригинал).")
        return

    page = await _telegraph_page(item.title, text, item.author, item.url)
    if page:
        await cq.message.answer(
            f"📄 <a href=\"{page}\">{_esc(item.title or 'Статья')}</a> — читалка"
        )
    else:  # fallback — текстом частями
        for chunk in _chunks(f"📄 <b>{_esc(item.title or '')}</b>\n\n{_esc(text)}", 4000):
            await cq.message.answer(chunk)


@dp.callback_query(F.data.startswith("rz:"))
async def on_razbor(cq: CallbackQuery) -> None:
    """📝 Разбор: структурированное саммари на русском (суть + технологии + поинты) как telegra.ph."""
    try:
        item_id = int(cq.data.split(":")[1])
    except (ValueError, IndexError):
        await cq.answer("bad")
        return
    item = await _repo.get_by_id(item_id)
    if item is None:
        await cq.answer("нет такой статьи")
        return

    await cq.answer("Разбираю…")
    text = await _article_text(item, item_id)
    if not text:
        await cq.message.answer("Полного текста нет — разбирать нечего, жми 🔗 (оригинал).")
        return

    try:
        body = await build_razbor(item.title, text)
    except Exception as e:
        log.warning("razbor_failed", news_item_id=item_id, error=str(e)[:120])
        await cq.message.answer("Не смог сделать разбор (LLM недоступна). Попробуй позже.")
        return

    page = await _telegraph_page(f"Разбор: {item.title or 'новость'}", body, "Homyak", item.url)
    if page:
        await cq.message.answer(
            f"📝 <a href=\"{page}\">Разбор: {_esc(item.title or 'новость')}</a>"
        )
    else:  # telegra.ph недоступен → отдаём текстом
        for chunk in _chunks(f"📝 <b>Разбор</b>\n\n{_esc(body)}", 4000):
            await cq.message.answer(chunk)


@dp.callback_query(F.data.startswith("fb:"))
async def on_feedback(cq: CallbackQuery) -> None:
    try:
        _, sig, sid = cq.data.split(":")
        item_id = int(sid)
    except (ValueError, AttributeError):
        await cq.answer("bad")
        return

    if sig == "mute":
        # Раньше здесь молча мьютился meta[2][0] — ПЕРВЫЙ тег статьи. А первый тег самый
        # широкий: у медицинской статьи это `medical`, и одно нажатие выключило вертикаль
        # целиком (273 айтема, 21%). Теперь спрашиваем, ЧТО именно мьютить.
        meta = await _repo.get_item_meta(item_id)
        tags = list(meta[2] or []) if meta else []
        if not tags:
            await cq.answer("у статьи нет тегов — мьютить нечего")
            return
        rows = [
            [InlineKeyboardButton(text=f"🔇 {t}", callback_data=f"mt:{item_id}:{i}")]
            for i, t in enumerate(tags[:6])
        ]
        # Кнопку-ссылку тащим в пикер: второй шаг ищет url именно в этой клавиатуре, и без
        # переноса «🔗 Источник» терялся бы навсегда — и на отмене, и после выбора тега.
        tail = [InlineKeyboardButton(text="✕ отмена", callback_data=f"mt:{item_id}:-1")]
        for row in (cq.message.reply_markup.inline_keyboard if cq.message and cq.message.reply_markup else []):
            for b in row:
                if b.url:
                    tail.append(b)
        rows.append(tail)
        await cq.answer("что мьютим?")
        with suppress(Exception):
            await cq.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
        return

    label = {"up": "👍", "down": "👎", "save": "⭐"}.get(sig, "?")
    result, _ = await _repo.record_feedback(item_id, sig, None)
    if _bus is not None:
        await _bus.publish_feedback(item_id, sig, None, action=result)
    status = f"{label} учтено" if result == "added" else f"{label} отменено"
    await cq.answer(status)

    # схлопываем реакции, чтобы не нажать повторно: остаётся статус + ссылка
    open_btn = None
    if cq.message and cq.message.reply_markup:
        for row in cq.message.reply_markup.inline_keyboard:
            for b in row:
                if b.url:
                    open_btn = b
    rows = [[InlineKeyboardButton(text=f"✓ {status}", callback_data="noop")]]
    tail = [
        InlineKeyboardButton(text="📝 Разбор", callback_data=f"rz:{item_id}"),
        InlineKeyboardButton(text="📄 текст", callback_data=f"txt:{item_id}"),
    ]
    if open_btn is not None:
        tail.append(open_btn)
    rows.append(tail)
    with suppress(Exception):
        await cq.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("mt:"))
async def on_mute_pick(cq: CallbackQuery) -> None:
    """Второй шаг 🔇: пользователь выбрал КОНКРЕТНЫЙ тег (или отменил)."""
    try:
        _, sid, sidx = cq.data.split(":")
        item_id, idx = int(sid), int(sidx)
    except (ValueError, AttributeError):
        await cq.answer("bad")
        return

    meta = await _repo.get_item_meta(item_id)
    url = None
    if cq.message and cq.message.reply_markup:  # ссылку на оригинал вернём из старой клавиатуры
        for row in cq.message.reply_markup.inline_keyboard:
            for b in row:
                if b.url:
                    url = b.url

    if idx < 0:  # отмена → возвращаем обычные кнопки
        await cq.answer("отменено")
        with suppress(Exception):
            await cq.message.edit_reply_markup(reply_markup=_kb(item_id, url))
        return

    tags = list(meta[2] or []) if meta else []
    if idx >= len(tags):
        await cq.answer("тег пропал — открой карточку заново")
        return
    topic = tags[idx]

    result, _ = await _repo.record_feedback(item_id, "mute_topic", topic)
    if _bus is not None:
        await _bus.publish_feedback(item_id, "mute_topic", topic, action=result)
    status = f"🔇 «{topic}» " + ("замьючено" if result == "added" else "снято")
    await cq.answer(status)

    rows = [[InlineKeyboardButton(text=f"✓ {status}", callback_data="noop")]]
    tail = [
        InlineKeyboardButton(text="📝 Разбор", callback_data=f"rz:{item_id}"),
        InlineKeyboardButton(text="📄 текст", callback_data=f"txt:{item_id}"),
    ]
    if url:
        tail.append(InlineKeyboardButton(text="🔗 Источник", url=url))
    rows.append(tail)
    with suppress(Exception):
        await cq.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# --- profile refinement (предложения правок профиля) ---

def _suggestion_kb(vertical: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Применить", callback_data=f"pr:apply:{vertical}"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"pr:reject:{vertical}"),
            ]
        ]
    )


async def handle_suggestion(data: dict) -> None:
    chat = await _repo.get_cursor(CHAT_KEY)
    if not chat:
        return
    vertical = data.get("vertical", "it")
    desc = data.get("description", "")
    topics = data.get("topics", []) or []
    loves = [t["name"] for t in topics if t.get("polarity") in ("love", "like")]
    mutes = [t["name"] for t in topics if t.get("polarity") == "mute"]
    text = (
        f"💡 <b>Уточнить профиль {LABELS.get(vertical, vertical)}</b> (по твоим 👍/👎)\n\n"
        f"{_esc(desc)}\n\n👍 {_esc(', '.join(loves)) or '—'}\n🔇 {_esc(', '.join(mutes)) or '—'}"
    )
    await _bot.send_message(int(chat), text, reply_markup=_suggestion_kb(vertical))


@dp.callback_query(F.data.startswith("pr:"))
async def on_profile_suggestion(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    action = parts[1]
    vertical = parts[2] if len(parts) > 2 else "it"
    key = f"profile:pending:{vertical}"
    pending = await _repo.get_cursor(key)
    if not pending:
        await cq.answer("Предложение устарело")
        with suppress(Exception):
            await cq.message.edit_reply_markup(reply_markup=None)
        return
    if action == "apply":
        data = json.loads(pending)
        version = await _repo.set_profile(vertical, data["description"], data.get("topics", []))
        await _repo.save_cursor(key, "")
        await cq.answer(f"{LABELS.get(vertical, vertical)} обновлён (v{version})")
        with suppress(Exception):
            await cq.message.edit_text(cq.message.html_text + f"\n\n✅ <b>Применено (v{version})</b>")
    else:
        await _repo.save_cursor(key, "")
        await cq.answer("Отклонено")
        with suppress(Exception):
            await cq.message.edit_text(cq.message.html_text + "\n\n❌ Отклонено")


# --- push loop ---


async def _maybe_push(item_id: int) -> None:
    chat = await _repo.get_cursor(CHAT_KEY)
    if not chat:
        return
    paused = await _repo.get_cursor(PAUSED_KEY)
    if paused and time.time() < float(paused):
        return
    if _in_quiet(datetime.now().hour, settings.quiet_hours):
        return
    if await _repo.count_pushed_since(60) >= settings.max_push_per_hour:
        return
    item = await _repo.get_by_id(item_id)
    if item is None or item.personal_score is None or item.pushed_at is not None:
        return
    allow = await _repo.get_cursor(PUSH_VERTICALS_KEY)  # скоуп вертикалей (пусто = все)
    if allow and (item.vertical or "") not in set(allow.split(",")):
        return
    thr = await _repo.get_cursor(THRESHOLD_KEY)
    threshold = float(thr) if thr else interest_weights().push_threshold
    if item.personal_score < threshold:
        return
    await _bot.send_message(int(chat), _fmt(item), reply_markup=_kb(item.id, item.url))
    await _repo.mark_pushed(item.id)
    log.info("pushed", item=item.id, score=round(item.personal_score, 3))


async def push_loop() -> None:
    sub = await _bus.js.subscribe(
        SUBJECT_PROCESSED, config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW)
    )
    log.info("push_loop_started")
    while True:
        try:
            msg = await sub.next_msg(timeout=30)
        except Exception:
            continue
        with suppress(Exception):
            await msg.ack()
            data = json.loads(msg.data)
            await _maybe_push(data["news_item_id"])


def _parse_allowed(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("tg_bad_allowed_id", value=part)
    return frozenset(ids)


class AllowlistMiddleware(BaseMiddleware):
    """Гейт: пропускает апдейты только от разрешённых user id, остальное молча роняет.

    Приватный бот. Без гейта любой, кто знает @username, читает твои ленты, портит обучение
    и /start'ом перезаписывает chat_id пушей на себя. Fail-closed: пустой allowlist = никого.
    Молчим намеренно — не подтверждаем чужому, что бот вообще существует.
    """

    def __init__(self, allowed: frozenset[int]) -> None:
        self._allowed = allowed

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in self._allowed:
            log.warning(
                "tg_unauthorized",
                user_id=(user.id if user else None),
                username=(user.username if user else None),
            )
            return None  # хендлер не вызываем — апдейт отброшен
        return await handler(event, data)


async def main_async() -> None:
    global _bus, _bot
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")
    allowed = _parse_allowed(settings.telegram_allowed_ids)
    if not allowed:
        log.error("tg_allowlist_empty", effect="бот никого не пустит (fail-closed) — задай TELEGRAM_ALLOWED_IDS")
    dp.update.outer_middleware(AllowlistMiddleware(allowed))
    log.info("tg_allowlist", allowed=sorted(allowed))
    _bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await _bot.set_my_commands(BOT_COMMANDS)
    _bus = NatsBus(settings.nats_url)
    await _bus.connect()
    push_task = asyncio.create_task(push_loop())
    suggest_task = asyncio.create_task(_bus.consume_profile_suggestion(handle_suggestion))
    log.info("tgbot_started")
    try:
        await dp.start_polling(_bot)
    finally:
        push_task.cancel()
        suggest_task.cancel()
        with suppress(asyncio.CancelledError):
            await push_task
            await suggest_task
        await _bus.close()
        await _bot.session.close()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
