"""RSS 2.0 рендер ленты через feedgen. Отдаёт representatives кластеров."""

from __future__ import annotations

from feedgen.feed import FeedGenerator

from homyak.core.interfaces import Feed

_BASE = "http://localhost:8000"
_SELF = _BASE + "/feed.rss"


def render(feed: Feed, title: str = "Homyak Feed", self_path: str = "/feed.rss") -> bytes:
    """self_path — путь ЭТОЙ ленты: по atom:link rel=self ридер переподписывается,
    и общий на всех /feed.rss увёл бы подписчика ⭐-ленты на общую."""
    fg = FeedGenerator()
    fg.id(_BASE + "/")
    fg.title(title)
    fg.link(href=_BASE + self_path, rel="self")
    fg.link(href=_BASE + "/", rel="alternate")
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
