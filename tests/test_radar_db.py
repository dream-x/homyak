"""Радар против живой БД: кандидаты (⭐ + лента за месяц), снимок с фейковым fetch, скорость."""

from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from homyak.core.interfaces import NewsItemDTO
from homyak.core.models import Feedback, RepoStats
from homyak.core.radar import RateLimited, candidate_repos, radar, snapshot
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


async def test_candidates_with_a_snapshot_in_the_window_go_before_newer_ones(session_factory):
    """Бюджет меньше кандидатов (без токена): сперва замыкаем пары, потом сеем первые точки."""
    repo = NewsRepo(session_factory)
    await _item(repo, "seen", "https://github.com/seen/repo", days_ago=5)
    await _item(repo, "new", "https://github.com/new/repo")
    async with session_factory() as s:
        await s.execute(text("update news_items set fetched_at = now() - interval '5 days' where source_id = 'seen'"))
        s.add(RepoStats(repo="seen/repo", day=date.today() - timedelta(days=1), stars=1))
        await s.commit()
    assert await candidate_repos(session_factory) == ["seen/repo", "new/repo"]


async def test_snapshot_and_radar(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    await _item(repo, "a", "https://github.com/a/fast", title="fast repo")
    await _item(repo, "b", "https://github.com/b/slow", title="slow repo")
    await _item(repo, "c", "https://github.com/c/gone", title="gone repo")
    await _item(repo, "a2", "https://GitHub.com/A/Fast", title="fast repo, other case")
    today = date.today()

    async def fake(repo_key: str):
        return {
            "a/fast": {"stars": 1000, "forks": 10, "open_issues": 1, "pushed_at": _NOW},
            "b/slow": {"stars": 500, "forks": 5, "open_issues": 0, "pushed_at": None},
        }.get(repo_key)

    r = await snapshot(session_factory, fetch=fake, today=today - timedelta(days=4))
    assert r == {"repos": 3, "saved": 2, "missing": 1, "errors": 0, "stopped": None}
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
    assert top[0]["item"]["title"].startswith("fast repo")  # ключ в SQL без учёта регистра
    assert top[0]["url"] == "https://github.com/a/fast"
    assert top[0]["starred"] is False


async def test_snapshot_stops_on_rate_limit(session_factory):
    repo = NewsRepo(session_factory)
    await _item(repo, "a", "https://github.com/a/one")
    await _item(repo, "b", "https://github.com/b/two")

    async def limited(repo_key: str):
        raise RateLimited("github rate limit: 403")

    r = await snapshot(session_factory, fetch=limited)
    assert r["saved"] == 0 and r["stopped"].startswith("github rate limit")


async def test_snapshot_skips_a_repo_that_fails_and_goes_on(session_factory):
    """Таймаут одного репозитория — не повод терять день для всех после него."""
    repo = NewsRepo(session_factory)
    for k in ("a", "b", "c"):
        await _item(repo, k, f"https://github.com/{k}/r")
    calls = []

    async def flaky(repo_key: str):
        calls.append(repo_key)
        if repo_key == "b/r":
            raise httpx.ReadTimeout("timed out")
        return {"stars": 1, "forks": 0, "open_issues": 0, "pushed_at": None}

    r = await snapshot(session_factory, fetch=flaky)
    assert sorted(calls) == ["a/r", "b/r", "c/r"]
    assert (r["saved"], r["errors"], r["stopped"]) == (2, 1, None)


async def test_snapshot_stops_after_an_error_streak(session_factory):
    repo = NewsRepo(session_factory)
    for k in "abcdefg":
        await _item(repo, k, f"https://github.com/{k}/r")

    async def down(repo_key: str):
        raise httpx.ConnectError("down")

    r = await snapshot(session_factory, fetch=down)
    assert r["errors"] == 5 and r["saved"] == 0 and "подряд" in r["stopped"]
