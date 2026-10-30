"""Карточка ручной ссылки рождается уже «после ⭐».

Звезду за принесённую ссылку ставит бот, а реакции в карточке — переключатели: живая кнопка ⭐
на такой карточке не подтверждала звезду, а снимала её (8 из 15 ручных ⭐ пропали из feedback,
оставшись в ⭐-канале и вике). Поэтому карточка шлётся сразу со схлопнутой клавиатурой — и из
`_star_manual`, и из `_maybe_push`, который доделывает отправку на редоставке после сбоя.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from homyak.adapters.outputs import tg_bot
from homyak.adapters.outputs.tg_bot import CHAT_KEY, _kb, _kb_done


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def _has_live_reactions(kb) -> bool:
    return any(c.startswith("fb:") for c in _callbacks(kb))


def test_done_keyboard_has_no_toggles_but_keeps_tools_and_link():
    kb = _kb_done(7, "⭐ учтено", "https://x.test/a")
    cbs = _callbacks(kb)
    assert not _has_live_reactions(kb)
    assert kb.inline_keyboard[0][0].text == "✓ ⭐ учтено"
    assert {"rz:7", "txt:7"} <= set(cbs)
    assert [b.url for row in kb.inline_keyboard for b in row if b.url] == ["https://x.test/a"]


def test_done_keyboard_without_url_has_no_link_button():
    kb = _kb_done(7, "👍 учтено", None)
    assert all(b.url is None for row in kb.inline_keyboard for b in row)


def test_regular_card_still_offers_star():
    assert "fb:save:7" in _callbacks(_kb(7, None))


class _Repo:
    """Память вместо Postgres: feedback — множество (item, signal), остальное — журналы."""

    def __init__(self, *, source_type: str = "manual", starred: bool = False):
        self.source_type = source_type
        self.feedback: set[tuple[int, str]] = {(7, "save")} if starred else set()
        self.recorded: list[tuple] = []
        self.pushed: list[int] = []

    async def get_by_id(self, item_id):
        return SimpleNamespace(
            id=item_id,
            source_type=self.source_type,
            url="https://x.test/a",
            vertical="it",
            feed_name=None,
            cluster_id=None,
            personal_score=0.9,
            pushed_at=None,
            processed_at=datetime.now(timezone.utc),
        )

    async def has_feedback(self, item_id, signal):
        return (item_id, signal) in self.feedback

    async def record_feedback(self, item_id, signal, topic=None):
        self.recorded.append((item_id, signal, topic))
        self.feedback.add((item_id, signal))
        return "added", topic

    async def get_cursor(self, key):
        return "123" if key == CHAT_KEY else None

    async def count_pushed_since(self, minutes):
        return 0

    async def cluster_already_sent(self, cluster_id, field, item_id):
        return False

    async def mark_pushed(self, item_id):
        self.pushed.append(item_id)


class _Bot:
    def __init__(self, *, fail_first: bool = False):
        self.sent: list[tuple] = []
        self._fail_first = fail_first

    async def send_message(self, chat, text, reply_markup=None):
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("bot api down")
        self.sent.append((chat, text, reply_markup))


class _Bus:
    def __init__(self):
        self.published: list[tuple] = []

    async def publish_feedback(self, *args, **kwargs):
        self.published.append((args, kwargs))


async def _noop(*args, **kwargs) -> None:
    return None


def _wire(monkeypatch, repo: _Repo, bot: _Bot | None = None):
    bot, bus = bot or _Bot(), _Bus()
    monkeypatch.setattr(tg_bot, "_repo", repo)
    monkeypatch.setattr(tg_bot, "_bot", bot)
    monkeypatch.setattr(tg_bot, "_bus", bus)
    monkeypatch.setattr(tg_bot, "_fmt", lambda item: "card")
    # пуш не должен зависеть от часа запуска тестов и от живого interests.yaml
    monkeypatch.setattr(tg_bot, "_in_quiet", lambda hour, quiet: False)
    monkeypatch.setattr(tg_bot, "interest_weights", lambda: SimpleNamespace(push_threshold=0.55))
    monkeypatch.setattr(tg_bot, "_publish_channel", _noop)
    return bot, bus


async def test_manual_card_is_sent_already_starred(monkeypatch):
    repo = _Repo()
    bot, bus = _wire(monkeypatch, repo)

    await tg_bot._star_manual(7)

    assert repo.recorded == [(7, "save", None)]
    assert bus.published and bus.published[0][1]["action"] == "added"
    assert repo.pushed == [7]
    ((chat, _, kb),) = bot.sent
    assert chat == 123
    assert not _has_live_reactions(kb)
    assert kb.inline_keyboard[0][0].text == "✓ ⭐ учтено"
    assert [b.url for row in kb.inline_keyboard for b in row if b.url] == ["https://x.test/a"]


async def test_redelivery_does_not_touch_an_existing_star(monkeypatch):
    repo = _Repo(starred=True)
    bot, bus = _wire(monkeypatch, repo)

    await tg_bot._star_manual(7)

    assert repo.recorded == []
    assert bus.published == []
    assert bot.sent == []


async def test_redelivery_after_failed_send_hands_out_no_live_star(monkeypatch):
    """Первая доставка: звезда записана, карточка не ушла (Bot API упал) → nak.

    Редоставка: `_star_manual` видит звезду и молчит, карточку досылает `_maybe_push` —
    и она обязана быть «после ⭐», иначе исходный баг возвращается с чёрного хода.
    """
    repo = _Repo()
    bot, bus = _wire(monkeypatch, repo, _Bot(fail_first=True))

    with pytest.raises(RuntimeError):
        await tg_bot.handle_processed({"news_item_id": 7})
    assert repo.recorded == [(7, "save", None)]
    assert repo.pushed == []
    assert bot.sent == []

    await tg_bot.handle_processed({"news_item_id": 7})

    assert repo.recorded == [(7, "save", None)]  # звезда и событие не задвоились
    assert len(bus.published) == 1
    assert repo.pushed == [7]
    ((_, _, kb),) = bot.sent
    assert not _has_live_reactions(kb)
    assert kb.inline_keyboard[0][0].text == "✓ ⭐ учтено"


async def test_unstarred_push_keeps_the_live_star(monkeypatch):
    repo = _Repo(source_type="rss")
    bot, _ = _wire(monkeypatch, repo)

    await tg_bot._maybe_push(7)

    ((_, _, kb),) = bot.sent
    assert "fb:save:7" in _callbacks(kb)
