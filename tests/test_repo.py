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
