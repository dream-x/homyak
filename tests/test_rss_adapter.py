from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import respx

from homyak.adapters.sources.rss import RSSSource
from homyak.core.config import RSSFeedConfig

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(hours=5)
NEW = NOW - timedelta(hours=1)
DAY = 86400  # интервал фида → окно 36ч, оба поста внутри (проверяем курсор, не свежесть)


def _rss(*items) -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>https://example.com/{s}</link>"
        f"<guid>https://example.com/{s}</guid><pubDate>{format_datetime(d)}</pubDate></item>"
        for t, s, d in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>'
        f"<link>https://example.com</link><description>d</description>{body}</channel></rss>"
    ).encode()


RSS_XML = _rss(("Old post", "old", OLD), ("New post", "new", NEW))


@respx.mock
async def test_rss_parses_and_advances_cursor():
    respx.get("https://example.com/rss").mock(return_value=httpx.Response(200, content=RSS_XML))
    src = RSSSource(
        RSSFeedConfig(name="t", url="https://example.com/rss", category="tech", interval_seconds=DAY)
    )

    results = [(dto, cur) async for dto, cur in src.poll(None)]

    assert [d.title for d, _ in results] == ["Old post", "New post"]  # отсортировано по дате ASC
    first = results[0][0]
    assert first.source_type == "rss"
    assert first.source_id == "https://example.com/old"
    assert first.category == "tech"
    # финальный курсор = дата самого свежего поста
    assert results[-1][1].startswith(NEW.strftime("%Y-%m-%d"))


@respx.mock
async def test_rss_cursor_skips_seen():
    respx.get("https://example.com/rss").mock(return_value=httpx.Response(200, content=RSS_XML))
    src = RSSSource(RSSFeedConfig(name="t", url="https://example.com/rss", interval_seconds=DAY))

    results = [(dto, cur) async for dto, cur in src.poll(OLD.isoformat())]

    assert [d.title for d, _ in results] == ["New post"]  # старый отфильтрован курсором


@respx.mock
async def test_rss_skips_stale_beyond_poll_window():
    """Прошлое не нужно: при первом контакте не тащим то, что старше окна поллинга."""
    respx.get("https://example.com/rss").mock(return_value=httpx.Response(200, content=RSS_XML))
    # интервал 10 мин → окно 15 мин; обоим постам 1ч и 5ч → не берём ничего
    src = RSSSource(RSSFeedConfig(name="t", url="https://example.com/rss", interval_seconds=600))

    results = [dto async for dto, _ in src.poll(None)]

    assert results == []
