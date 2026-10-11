"""Цикл пушей после обрыва прокси (29.09–11.10.2026).

Пока Bot API был недоступен, эфемерная подписка бота утонула в редоставках, а ошибки отправки
глотались. Тесты фиксируют новый контракт: durable-хендлер, отсечка старья, ошибка наружу.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from homyak.adapters.outputs import tg_bot
from homyak.adapters.outputs.tg_bot import PUSH_MAX_AGE, handle_processed, is_stale

NOW = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_fresh_item_is_not_stale():
    assert is_stale(NOW - timedelta(hours=1), NOW, PUSH_MAX_AGE) is False


def test_item_older_than_max_age_is_stale():
    assert is_stale(NOW - PUSH_MAX_AGE - timedelta(minutes=1), NOW, PUSH_MAX_AGE) is True


def test_naive_processed_at_is_read_as_utc():
    """В БД timestamptz, но на всякий случай naive трактуем как UTC, а не роняем сравнение."""
    assert is_stale(datetime(2026, 10, 11, 23, 30), NOW, PUSH_MAX_AGE) is False


def test_missing_processed_at_is_treated_as_fresh():
    """Нет отметки — не теряем item из-за отсутствия данных."""
    assert is_stale(None, NOW, PUSH_MAX_AGE) is False


class _Spy:
    def __init__(self, fail: Exception | None = None):
        self.calls: list[int] = []
        self.kwargs: list[dict] = []
        self._fail = fail

    async def __call__(self, item_id: int, **kw) -> None:
        self.calls.append(item_id)
        self.kwargs.append(kw)
        if self._fail:
            raise self._fail


def _wire(monkeypatch, processed_at, fail_channel: Exception | None = None):
    star, push, chan = _Spy(), _Spy(), _Spy(fail_channel)
    monkeypatch.setattr(tg_bot, "_star_manual", star)
    monkeypatch.setattr(tg_bot, "_maybe_push", push)
    monkeypatch.setattr(tg_bot, "_publish_channel", chan)

    async def get_by_id(item_id):
        return SimpleNamespace(id=item_id, processed_at=processed_at)

    monkeypatch.setattr(tg_bot, "_repo", SimpleNamespace(get_by_id=get_by_id))
    return star, push, chan


async def test_fresh_item_goes_through_all_three_steps(monkeypatch):
    star, push, chan = _wire(monkeypatch, datetime.now(timezone.utc))
    await handle_processed({"news_item_id": 7})
    assert star.calls == push.calls == chan.calls == [7]
    assert star.kwargs == [{"notify": True}]


async def test_stale_backlog_is_starred_but_neither_pushed_nor_posted(monkeypatch):
    """После долгого простоя durable отдаёт всё накопившееся — старьё в канал и личку не идёт.

    Звезда за ручную ссылку при этом ставится (выбор человека, обучение ранкера, вика), но без
    карточки в личку: DM двухнедельной давности — такой же залп, как и пуши.
    """
    star, push, chan = _wire(monkeypatch, datetime.now(timezone.utc) - timedelta(days=12))
    await handle_processed({"news_item_id": 7})
    assert star.calls == [7]
    assert star.kwargs == [{"notify": False}]
    assert push.calls == [] and chan.calls == []


async def test_send_failure_propagates_so_the_message_is_nakked(monkeypatch):
    """Ошибка отправки не глотается: consume делает nak, и после оживления прокси пост доедет."""
    _wire(monkeypatch, datetime.now(timezone.utc), fail_channel=RuntimeError("503 proxy"))
    with pytest.raises(RuntimeError, match="503"):
        await handle_processed({"news_item_id": 7})
