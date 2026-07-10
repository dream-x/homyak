import httpx
import respx

from homyak.core.config import RSSFeedConfig
from homyak.adapters.sources.rss import RSSSource

RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Test</title><link>https://example.com</link><description>d</description>
<item>
  <title>Old post</title>
  <link>https://example.com/old</link>
  <guid>https://example.com/old</guid>
  <pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate>
</item>
<item>
  <title>New post</title>
  <link>https://example.com/new</link>
  <guid>https://example.com/new</guid>
  <pubDate>Tue, 02 Jan 2024 10:00:00 GMT</pubDate>
</item>
</channel></rss>"""


@respx.mock
async def test_rss_parses_and_advances_cursor():
    respx.get("https://example.com/rss").mock(return_value=httpx.Response(200, content=RSS_XML))
    src = RSSSource(RSSFeedConfig(name="t", url="https://example.com/rss", category="tech"))

    results = [(dto, cur) async for dto, cur in src.poll(None)]

    assert [d.title for d, _ in results] == ["Old post", "New post"]  # отсортировано по дате ASC
    first = results[0][0]
    assert first.source_type == "rss"
    assert first.source_id == "https://example.com/old"
    assert first.category == "tech"
    # финальный курсор = дата самого свежего поста
    assert results[-1][1].startswith("2024-01-02")


@respx.mock
async def test_rss_cursor_skips_seen():
    respx.get("https://example.com/rss").mock(return_value=httpx.Response(200, content=RSS_XML))
    src = RSSSource(RSSFeedConfig(name="t", url="https://example.com/rss"))

    results = [(dto, cur) async for dto, cur in src.poll("2024-01-01T10:00:00+00:00")]

    assert [d.title for d, _ in results] == ["New post"]  # старый отфильтрован курсором
