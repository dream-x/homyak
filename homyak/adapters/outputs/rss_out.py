"""RSS 2.0 рендер ленты через feedgen. Отдаёт representatives кластеров."""

from __future__ import annotations

from feedgen.feed import FeedGenerator

from homyak.core.interfaces import Feed

_SELF = "http://localhost:8000/feed.rss"


def render(feed: Feed, title: str = "Homyak Feed") -> bytes:
    fg = FeedGenerator()
    fg.id("http://localhost:8000/")
    fg.title(title)
    fg.link(href=_SELF, rel="self")
    fg.link(href="http://localhost:8000/", rel="alternate")
    fg.description("Персональная лента новостей Homyak")
    fg.language("ru")

    for it in feed.items:
        fe = fg.add_entry()
        fe.id(str(it.id or it.source_id))
        fe.title(it.title or "(без заголовка)")
        if it.url:
            fe.link(href=it.url)
        if it.text:
            fe.description(it.text)
        if it.published_at:
            fe.published(it.published_at)
        if it.author:
            fe.author(name=it.author)

    return fg.rss_str(pretty=True)
