"""Карточка ручной ссылки рождается уже «после ⭐».

Звезду за принесённую ссылку ставит бот, а реакции в карточке — переключатели: живая кнопка ⭐
на такой карточке не подтверждала звезду, а снимала её (8 из 15 ручных ⭐ пропали из feedback,
оставшись в ⭐-канале и вике). Поэтому карточка шлётся сразу со схлопнутой клавиатурой.
"""

from types import SimpleNamespace

from homyak.adapters.outputs import tg_bot
from homyak.adapters.outputs.tg_bot import _kb, _kb_done


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def test_done_keyboard_has_no_toggles_but_keeps_tools_and_link():
    kb = _kb_done(7, "⭐ учтено", "https://x.test/a")
    cbs = _callbacks(kb)
    assert not any(c.startswith("fb:") for c in cbs)
    assert kb.inline_keyboard[0][0].text == "✓ ⭐ учтено"
    assert {"rz:7", "txt:7"} <= set(cbs)
    assert [b.url for row in kb.inline_keyboard for b in row if b.url] == ["https://x.test/a"]


def test_done_keyboard_without_url_has_no_link_button():
    kb = _kb_done(7, "👍 учтено", None)
    assert all(b.url is None for row in kb.inline_keyboard for b in row)


def test_regular_card_still_offers_star():
    assert "fb:save:7" in _callbacks(_kb(7, None))


class _Repo:
    def __init__(self, *, starred: bool = False):
        self.starred = starred
        self.recorded: list[tuple] = []
        self.pushed: list[int] = []

    async def get_by_id(self, item_id):
        return SimpleNamespace(
            id=item_id, source_type="manual", url="https://x.test/a", vertical="it"
        )

    async def has_feedback(self, item_id, signal):
        return self.starred

    async def record_feedback(self, item_id, signal, topic=None):
        self.recorded.append((item_id, signal, topic))
        return "added", topic

    async def get_cursor(self, key):
        return "123"

    async def mark_pushed(self, item_id):
        self.pushed.append(item_id)


class _Bot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat, text, reply_markup=None):
        self.sent.append((chat, text, reply_markup))


class _Bus:
    def __init__(self):
        self.published: list[tuple] = []

    async def publish_feedback(self, *args, **kwargs):
        self.published.append((args, kwargs))


def _wire(monkeypatch, repo: _Repo):
    bot, bus = _Bot(), _Bus()
    monkeypatch.setattr(tg_bot, "_repo", repo)
    monkeypatch.setattr(tg_bot, "_bot", bot)
    monkeypatch.setattr(tg_bot, "_bus", bus)
    monkeypatch.setattr(tg_bot, "_fmt", lambda item: "card")
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
    assert not any(c.startswith("fb:") for c in _callbacks(kb))
    assert kb.inline_keyboard[0][0].text == "✓ ⭐ учтено"
    assert [b.url for row in kb.inline_keyboard for b in row if b.url] == ["https://x.test/a"]


async def test_redelivery_does_not_touch_an_existing_star(monkeypatch):
    repo = _Repo(starred=True)
    bot, bus = _wire(monkeypatch, repo)

    await tg_bot._star_manual(7)

    assert repo.recorded == []
    assert bus.published == []
    assert bot.sent == []
