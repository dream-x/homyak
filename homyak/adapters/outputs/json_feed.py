"""JSON Feed 1.1 рендер (https://www.jsonfeed.org/version/1.1/)."""

from __future__ import annotations

from homyak.core.config import settings
from homyak.core.interfaces import Feed


def render(feed: Feed, title: str = "Homyak Feed", self_path: str = "/feed.json") -> dict:
    """self_path — путь ЭТОЙ ленты: feed_url ридер запоминает как адрес подписки."""
    base = settings.public_base_url.rstrip("/")
    items = []
    for it in feed.items:
        entry: dict = {"id": str(it.id or it.source_id)}
        if it.url:
            entry["url"] = it.url
        if it.title:
            entry["title"] = it.title
        if it.text:
            entry["content_text"] = it.text
        if it.published_at:
            entry["date_published"] = it.published_at.isoformat()
        if it.tags:
            entry["tags"] = it.tags
        if it.author:
            entry["authors"] = [{"name": it.author}]
        items.append(entry)

    result = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": title,
        "home_page_url": base + "/",
        "feed_url": base + self_path,
        "items": items,
    }
    if feed.next_cursor:
        result["next_url"] = f"{base}{self_path}?cursor={feed.next_cursor}"
    return result
