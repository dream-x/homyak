"""Радар против живой БД: кандидаты (⭐ + лента за месяц), снимок с фейковым fetch, скорость."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from homyak.core.interfaces import NewsItemDTO
from homyak.core.models import Feedback
from homyak.core.radar import candidate_repos, radar, snapshot
from homyak.storage.postgres import NewsRepo

_NOW = datetime.now(timezone.utc)


async def _item(repo, sid, url, days_ago=1, title=None):
    iid, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id=sid, url=url, feed_name="gh_trending",
                    published_at=_NOW - timedelta(days=days_ago), title=title or sid)
    )
    return iid


async def test_candidates_starred_first_then_recent_feed_repos(session_factory):
    repo = NewsRepo(session_factory)
    old = await _item(repo, "old", "https://github.com/Old/Repo")
    await _item(repo, "fresh", "https://github.com/Fresh/Repo/tree/main")
    await _item(repo, "fresh2", "https://github.com/fresh/repo")  # то же, другой регистр
    await _item(repo, "nope", "https://example.com/not-github")
    await _item(repo, "topic", "https://github.com/topics/llm")
    async with session_factory() as s:
        await s.execute(text("update news_items set fetched_at = now() - interval '45 days' where id = :i"), {"i": old})
        s.add(Feedback(news_item_id=old, signal="save", surface="tgbot"))
        await s.commit()

    assert await candidate_repos(session_factory) == ["old/repo", "fresh/repo"]


async def test_snapshot_and_radar(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    await _item(repo, "a", "https://github.com/a/fast", title="fast repo")
    await _item(repo, "b", "https://github.com/b/slow", title="slow repo")
    await _item(repo, "c", "https://github.com/c/gone", title="gone repo")
    today = date.today()

    async def fake(repo_key: str):
        return {
            "a/fast": {"stars": 1000, "forks": 10, "open_issues": 1, "pushed_at": _NOW},
            "b/slow": {"stars": 500, "forks": 5, "open_issues": 0, "pushed_at": None},
        }.get(repo_key)

    r = await snapshot(session_factory, fetch=fake, today=today - timedelta(days=4))
    assert r == {"repos": 3, "saved": 2, "missing": 1, "stopped": None}
    assert await radar(7, sf=session_factory) == []  # один снимок — скорости нет

    async def fake_later(repo_key: str):
        return {
            "a/fast": {"stars": 1800, "forks": 12, "open_issues": 1, "pushed_at": _NOW},
            "b/slow": {"stars": 520, "forks": 5, "open_issues": 0, "pushed_at": None},
        }.get(repo_key)

    await snapshot(session_factory, fetch=fake_later, today=today)
    await snapshot(session_factory, fetch=fake_later, today=today)  # повтор за день — перезапись

    top = await radar(7, sf=session_factory)
    assert [(t["repo"], t["stars"], t["gained"], t["per_day"], t["span_days"]) for t in top] == [
        ("a/fast", 1800, 800, 200.0, 4),
        ("b/slow", 520, 20, 5.0, 4),
    ]
    assert top[0]["item"]["title"] == "fast repo" and top[0]["url"] == "https://github.com/a/fast"
    assert top[0]["starred"] is False


async def test_snapshot_stops_on_rate_limit(session_factory):
    repo = NewsRepo(session_factory)
    await _item(repo, "a", "https://github.com/a/one")
    await _item(repo, "b", "https://github.com/b/two")

    async def limited(repo_key: str):
        raise RuntimeError("github rate limit: 403")

    r = await snapshot(session_factory, fetch=limited)
    assert r["saved"] == 0 and r["stopped"].startswith("github rate limit")
