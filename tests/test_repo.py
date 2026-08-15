from datetime import datetime, timezone

from homyak.core.interfaces import FeedQuery, NewsItemDTO
from homyak.core.models import NewsItem
from homyak.storage.postgres import NewsRepo


async def test_upsert_idempotent_and_normalizes_url(session_factory):
    repo = NewsRepo(session_factory)
    id1, new1 = await repo.upsert_item(
        NewsItemDTO(
            source_type="rss", source_id="x1", url="https://a.com/p?utm_source=x", title="T"
        )
    )
    assert new1 is True

    id2, new2 = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="x1", url="https://a.com/p", title="T2")
    )
    assert new2 is False
    assert id1 == id2

    row = await repo.get_by_id(id1)
    assert row.title == "T2"  # содержимое обновилось
    assert row.url_normalized == "https://a.com/p"  # tracking-параметр вырезан


async def test_feed_dto_carries_vertical(session_factory):
    # регресс: _dto должен пробрасывать vertical, иначе рендер ленты падает в _fmt
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="v1", url="https://e.com/v1", title="V")
    )
    async with session_factory() as s:
        item = await s.get(NewsItem, id_)
        item.vertical = "it"
        item.personal_score = 0.9
        item.processed_at = datetime.now(timezone.utc)
        await s.commit()

    feed = await repo.feed(FeedQuery(sort="personal", vertical="it", limit=5))
    assert feed.items
    assert feed.items[0].vertical == "it"


async def test_cursor_roundtrip(session_factory):
    repo = NewsRepo(session_factory)
    assert await repo.get_cursor("s") is None
    await repo.save_cursor("s", "42")
    assert await repo.get_cursor("s") == "42"
    await repo.save_cursor("s", "43", error="boom")
    assert await repo.get_cursor("s") == "43"


async def test_sweeper_recovers_items_killed_by_infra_outage(session_factory):
    """Регресс 28.07: сутки без LLM → attempts исчерпаны → item выпал и из JetStream, и из
    выборки sweeper'а, то есть умер навсегда из-за чужого сбоя. Вторая линия его возвращает."""
    from datetime import datetime, timedelta, timezone

    repo = NewsRepo(session_factory)
    ids = {}
    for key in ("fresh", "outage", "hopeless"):
        id_, _ = await repo.upsert_item(
            NewsItemDTO(source_type="rss", source_id=f"sw-{key}", url=f"https://e.com/{key}", title=key)
        )
        ids[key] = id_

    old = datetime.now(timezone.utc) - timedelta(hours=6)
    async with session_factory() as s:
        for key, attempts in (("fresh", 2), ("outage", 7), ("hopeless", 25)):
            item = await s.get(NewsItem, ids[key])
            item.fetched_at = old
            item.attempts = attempts
            item.retry_after = old  # отлежались: время попытки давно прошло
        await s.commit()

    stale = await repo.unprocessed_stale(older_than_minutes=5)
    assert ids["fresh"] in stale       # обычная линия — не исчерпал попытки
    assert ids["outage"] in stale      # вторая линия — жертва инфраструктурного сбоя
    assert ids["hopeless"] not in stale  # за RECOVERY_CAP: это уже битый item, не инфра


async def test_recovery_line_waits_before_retrying(session_factory):
    """Исчерпавший попытки не должен дёргаться сразу — даём инфраструктуре подняться."""
    from datetime import datetime, timedelta, timezone

    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="sw-justfailed", url="https://e.com/jf", title="jf")
    )
    async with session_factory() as s:
        item = await s.get(NewsItem, id_)
        item.fetched_at = datetime.now(timezone.utc) - timedelta(hours=6)
        item.attempts = 7
        item.retry_after = datetime.now(timezone.utc) - timedelta(minutes=1)  # упал только что
        await s.commit()

    assert id_ not in await repo.unprocessed_stale(older_than_minutes=5)


async def test_sweeper_does_not_hand_out_the_same_item_twice(session_factory):
    """Главная защита от лавины: выданное уходит на паузу, а не выгребается каждые 5 минут.

    Без неё sweeper брал одни и те же записи 288 раз в сутки по 500 штук — у консюмера
    накопилось 266 000 сообщений на 4872 реальные записи, и свежая новость вставала в хвост
    за четверть миллиона дублей.
    """
    from datetime import datetime, timedelta, timezone

    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="sw-dup", url="https://e.com/dup", title="dup")
    )
    async with session_factory() as s:
        item = await s.get(NewsItem, id_)
        item.fetched_at = datetime.now(timezone.utc) - timedelta(hours=6)
        await s.commit()

    assert id_ in await repo.unprocessed_stale(older_than_minutes=5)
    assert id_ not in await repo.unprocessed_stale(older_than_minutes=5)  # сразу — уже на паузе

    async with session_factory() as s:  # пауза вышла — возвращается
        item = await s.get(NewsItem, id_)
        item.retry_after = datetime.now(timezone.utc) - timedelta(minutes=1)
        await s.commit()
    assert id_ in await repo.unprocessed_stale(older_than_minutes=5)


async def test_sweeper_takes_the_freshest_first(session_factory):
    """Лента новостей: при заторе FIFO кормит процессор архивом, и сегодняшнее не доходит."""
    from datetime import datetime, timedelta, timezone

    repo = NewsRepo(session_factory)
    ids = []
    for i in range(4):
        id_, _ = await repo.upsert_item(
            NewsItemDTO(source_type="rss", source_id=f"sw-ord{i}", url=f"https://e.com/o{i}", title=f"o{i}")
        )
        ids.append(id_)
    async with session_factory() as s:
        for id_ in ids:
            item = await s.get(NewsItem, id_)
            item.fetched_at = datetime.now(timezone.utc) - timedelta(hours=6)
        await s.commit()

    assert await repo.unprocessed_stale(older_than_minutes=5) == sorted(ids, reverse=True)
