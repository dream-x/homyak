from homyak.core.interfaces import NewsItemDTO
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


async def test_cursor_roundtrip(session_factory):
    repo = NewsRepo(session_factory)
    assert await repo.get_cursor("s") is None
    await repo.save_cursor("s", "42")
    assert await repo.get_cursor("s") == "42"
    await repo.save_cursor("s", "43", error="boom")
    assert await repo.get_cursor("s") == "43"
