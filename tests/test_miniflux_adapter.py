import httpx
import respx

from homyak.core.config import MinifluxConfig
from homyak.adapters.sources.miniflux import MinifluxSource

ENTRIES = {
    "total": 2,
    "entries": [
        {
            "id": 10,
            "url": "https://example.com/a",
            "title": "A",
            "content": "text a",
            "author": "auth",
            "published_at": "2024-01-01T10:00:00Z",
            "feed": {"title": "Feed1", "category": {"title": "Tech"}},
        },
        {
            "id": 12,
            "url": "https://example.com/b",
            "title": "B",
            "content": "text b",
            "published_at": "2024-01-02T10:00:00Z",
            "feed": {"title": "Feed2", "category": {"title": "AI"}},
        },
    ],
}


@respx.mock
async def test_miniflux_parses_and_cursor_is_max_id():
    respx.get("http://mf:8080/v1/entries").mock(
        return_value=httpx.Response(200, json=ENTRIES)
    )
    src = MinifluxSource(MinifluxConfig(enabled=True, base_url="http://mf:8080"))

    results = [(dto, cur) async for dto, cur in src.poll(None)]

    assert [d.title for d, _ in results] == ["A", "B"]
    assert results[0][0].source_type == "miniflux"
    assert results[0][0].source_id == "10"
    assert results[-1][1] == "12"  # курсор = max entry.id


@respx.mock
async def test_miniflux_category_filter():
    respx.get("http://mf:8080/v1/entries").mock(
        return_value=httpx.Response(200, json=ENTRIES)
    )
    src = MinifluxSource(
        MinifluxConfig(enabled=True, base_url="http://mf:8080", categories=["ai"])
    )

    results = [(dto, cur) async for dto, cur in src.poll(None)]

    assert [d.title for d, _ in results] == ["B"]  # только AI-категория
    assert results[-1][1] == "12"  # курсор всё равно проехал мимо отфильтрованного
