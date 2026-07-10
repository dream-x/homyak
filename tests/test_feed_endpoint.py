from datetime import datetime, timedelta, timezone

import httpx
from httpx import ASGITransport

from homyak.adapters.analyzers.url_dedup import UrlDedupAnalyzer
from homyak.adapters.outputs import api
from homyak.core.interfaces import AnalyzerContext, NewsItemDTO
from homyak.core.models import NewsItem
from homyak.storage.postgres import NewsRepo

_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


async def _seed(session_factory, repo, source_id, url, minutes):
    id_, _ = await repo.upsert_item(
        NewsItemDTO(
            source_type="rss",
            source_id=source_id,
            url=url,
            title=source_id.upper(),
            published_at=_BASE + timedelta(minutes=minutes),
            category="tech",
        )
    )
    analyzer = UrlDedupAnalyzer()
    async with session_factory() as s:
        item = await s.get(NewsItem, id_)
        ctx = AnalyzerContext(item_id=id_, item=item, session=s)
        await analyzer.analyze(ctx)
        await s.commit()
    await repo.mark_processed(id_, cluster_id=ctx.cluster_id, category="tech")
    return id_


async def _client():
    return httpx.AsyncClient(transport=ASGITransport(app=api.app), base_url="http://t")


async def test_feed_collapses_clusters(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    monkeypatch.setattr(api, "repo", repo)
    # a и dup — один URL (склеятся), c — отдельный
    await _seed(session_factory, repo, "a", "https://x.com/p", 10)
    await _seed(session_factory, repo, "dup", "https://www.x.com/p/?utm_source=y", 20)
    await _seed(session_factory, repo, "c", "https://x.com/other", 30)

    async with await _client() as ac:
        r = await ac.get("/feed?limit=10")
        assert r.status_code == 200
        data = r.json()
        # 3 item'а, но 2 схлопнулись в один кластер → 2 представителя
        assert len(data["items"]) == 2
        # порядок — по свежести (c новее)
        assert data["items"][0]["title"] == "C"


async def test_feed_rss_and_json_valid(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    monkeypatch.setattr(api, "repo", repo)
    await _seed(session_factory, repo, "a", "https://x.com/p", 10)

    async with await _client() as ac:
        rss = await ac.get("/feed.rss")
        assert rss.status_code == 200
        assert b"<rss" in rss.content

        jf = await ac.get("/feed.json")
        assert jf.status_code == 200
        assert jf.json()["version"] == "https://jsonfeed.org/version/1.1"
        assert len(jf.json()["items"]) == 1


async def test_feed_pagination_cursor(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    monkeypatch.setattr(api, "repo", repo)
    for i in range(5):
        await _seed(session_factory, repo, f"i{i}", f"https://x.com/{i}", i)

    async with await _client() as ac:
        page1 = (await ac.get("/feed?limit=2")).json()
        assert len(page1["items"]) == 2
        assert page1["next_cursor"]
        page2 = (await ac.get(f"/feed?limit=2&cursor={page1['next_cursor']}")).json()
        assert len(page2["items"]) == 2
        # без пересечений
        ids1 = {i["id"] for i in page1["items"]}
        ids2 = {i["id"] for i in page2["items"]}
        assert ids1.isdisjoint(ids2)
