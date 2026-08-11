"""API выгрузки помеченного ⭐/👍 (/saved)."""

from datetime import datetime, timedelta, timezone

import httpx
from httpx import ASGITransport

from homyak.adapters.outputs import api
from homyak.core.interfaces import NewsItemDTO
from homyak.core.models import NewsItem
from homyak.storage.postgres import NewsRepo

_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


async def _seed(session_factory, repo, source_id, *, minutes=0, score=None, tags=None,
                vertical=None):
    id_, _ = await repo.upsert_item(
        NewsItemDTO(
            source_type="rss",
            source_id=source_id,
            url=f"https://x.com/{source_id}",
            title=source_id.upper(),
            published_at=_BASE + timedelta(minutes=minutes),
        )
    )
    async with session_factory() as s:
        item = await s.get(NewsItem, id_)
        item.personal_score = score
        item.tags = tags or []
        item.vertical = vertical
        await s.commit()
    return id_


async def _client():
    return httpx.AsyncClient(transport=ASGITransport(app=api.app), base_url="http://t")


async def test_saved_returns_only_starred(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    monkeypatch.setattr(api, "repo", repo)
    starred = await _seed(session_factory, repo, "a")
    await _seed(session_factory, repo, "b")  # без фидбека
    liked = await _seed(session_factory, repo, "c")
    await repo.record_feedback(starred, "save")
    await repo.record_feedback(liked, "up")

    async with await _client() as ac:
        data = (await ac.get("/saved")).json()
        assert [i["id"] for i in data["items"]] == [starred]
        assert data["items"][0]["signals"] == ["save"]
        assert data["total"] == 1

        both = (await ac.get("/saved?signal=any")).json()
        assert {i["id"] for i in both["items"]} == {starred, liked}


async def test_saved_dedups_multiple_signals_on_one_item(session_factory, monkeypatch):
    """⭐ и 👍 на одной записи не должны размножить её в выдаче."""
    repo = NewsRepo(session_factory)
    monkeypatch.setattr(api, "repo", repo)
    id_ = await _seed(session_factory, repo, "a")
    await repo.record_feedback(id_, "save")
    await repo.record_feedback(id_, "up")
    await repo.record_feedback(id_, "mute_topic", "llm")  # не сигнал сохранения

    async with await _client() as ac:
        data = (await ac.get("/saved?signal=any")).json()
        assert len(data["items"]) == 1
        assert data["items"][0]["signals"] == ["save", "up"]


async def test_saved_sort_and_filters(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    monkeypatch.setattr(api, "repo", repo)
    low = await _seed(session_factory, repo, "low", score=0.3, tags=["llm"], vertical="it")
    high = await _seed(session_factory, repo, "high", score=0.9, tags=["bio"], vertical="medical")
    await repo.record_feedback(low, "save")  # помечен раньше
    await repo.record_feedback(high, "save")

    async with await _client() as ac:
        by_saved = (await ac.get("/saved")).json()
        assert [i["id"] for i in by_saved["items"]] == [high, low]  # свежая пометка первой

        by_score = (await ac.get("/saved?sort=score")).json()
        assert [i["id"] for i in by_score["items"]] == [high, low]

        assert [i["id"] for i in (await ac.get("/saved?tag=llm")).json()["items"]] == [low]
        assert [i["id"] for i in (await ac.get("/saved?kind=medical")).json()["items"]] == [high]
        assert [i["id"] for i in (await ac.get("/saved?min_score=0.5")).json()["items"]] == [high]


async def test_saved_since_is_incremental(session_factory, monkeypatch):
    """`since` отдаёт только помеченное позже — это опора инкрементальной выгрузки."""
    repo = NewsRepo(session_factory)
    monkeypatch.setattr(api, "repo", repo)
    first = await _seed(session_factory, repo, "a")
    await repo.record_feedback(first, "save")

    async with await _client() as ac:
        page = (await ac.get("/saved")).json()
        watermark = page["latest_saved_at"]
        assert (await ac.get(f"/saved?since={watermark}")).json()["items"] == []

        second = await _seed(session_factory, repo, "b")
        await repo.record_feedback(second, "save")
        fresh = (await ac.get(f"/saved?since={watermark}")).json()
        assert [i["id"] for i in fresh["items"]] == [second]

        # '+' в query-string приезжает пробелом — водяной знак обязан пережить и это
        spoiled = (
            await ac.get("/saved", params={"since": watermark.replace("+", " ")})
        ).json()
        assert [i["id"] for i in spoiled["items"]] == [second]
        assert (await ac.get("/saved?since=вчера")).status_code == 400


async def test_saved_feeds_render(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    monkeypatch.setattr(api, "repo", repo)
    id_ = await _seed(session_factory, repo, "a")
    await repo.record_feedback(id_, "save")

    async with await _client() as ac:
        rss = await ac.get("/saved.rss")
        assert rss.status_code == 200 and b"<rss" in rss.content
        # rel=self — адрес подписки: ридер не должен увести подписчика ⭐ на общую ленту
        assert b'/saved.rss" rel="self"' in rss.content
        jf = (await ac.get("/saved.json")).json()
        assert jf["version"] == "https://jsonfeed.org/version/1.1"
        assert jf["feed_url"].endswith("/saved.json")
        assert len(jf["items"]) == 1


async def test_saved_rejects_bad_params(session_factory, monkeypatch):
    monkeypatch.setattr(api, "repo", NewsRepo(session_factory))
    async with await _client() as ac:
        assert (await ac.get("/saved?signal=down")).status_code == 400
        assert (await ac.get("/saved?sort=random")).status_code == 400
