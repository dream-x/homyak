"""Таймлайн против живой БД: окна с нулями, одна история — один счёт, три пути (watch/tag/текст)
объединяются, лучшая запись окна — с максимальным personal_score."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import update

from homyak.core.interfaces import NewsItemDTO
from homyak.core.models import Cluster, NewsItem
from homyak.core.timeline import timeline
from homyak.storage.postgres import NewsRepo

_NOW = datetime.now(timezone.utc)


async def _item(session_factory, repo, sid, *, days_ago, title, tags=(), watch=(), score=None,
                cluster=None, text=""):
    iid, _ = await repo.upsert_item(
        NewsItemDTO(
            source_type="rss", source_id=sid, url=f"https://x.test/{sid}",
            published_at=_NOW - timedelta(days=days_ago), title=title, text=text,
        )
    )
    async with session_factory() as s:
        if cluster is None:
            c = Cluster(representative_id=iid, size=1)
            s.add(c)
            await s.flush()
            cluster = c.id
        await s.execute(
            update(NewsItem).where(NewsItem.id == iid).values(
                cluster_id=cluster, tags=list(tags), watch_topics=list(watch), vertical="it",
                processed_at=_NOW, personal_score=score,
            )
        )
        await s.commit()
    return iid, cluster


async def test_timeline_counts_stories_per_window_through_every_path(session_factory, monkeypatch):
    from homyak.core import timeline as tl
    from homyak.core.interests import WatchTopic

    topics = [WatchTopic(name="Iran", aliases=["иран", "tehran"])]
    monkeypatch.setattr(tl, "load_interests", lambda: SimpleNamespace(watch=topics))
    repo = NewsRepo(session_factory)
    # неделя 0 (сейчас): watch-запись + её кластерный близнец (одна история) + текстовая
    a, c = await _item(session_factory, repo, "a", days_ago=1, title="Iran talks", watch=["Iran"], score=0.6)
    await _item(session_factory, repo, "a2", days_ago=1, title="Iran talks (TG)", watch=["Iran"], score=0.9, cluster=c)
    await _item(session_factory, repo, "b", days_ago=2, title="Tehran markets", text="Иранский риал упал", score=0.3)
    # 3 недели назад: по тегу
    await _item(session_factory, repo, "d", days_ago=22, title="sanctions", tags=["Iran"], score=0.5)
    # мимо: другая тема
    await _item(session_factory, repo, "z", days_ago=3, title="rust release", tags=["rust"], score=0.8)

    r = await timeline("иран", days=30, sf=session_factory)

    assert r["modes"] == ["watch", "tag", "text"] and r["watch"] == "Iran" and r["unit"] == "day"
    assert r["total"] == 3  # a+a2 — одна история, b, d
    assert len(r["buckets"]) == 31  # окна с нулями
    by_day = {b["start"][:10]: b for b in r["buckets"]}
    d1 = (_NOW - timedelta(days=1)).date().isoformat()
    d2 = (_NOW - timedelta(days=2)).date().isoformat()
    d22 = (_NOW - timedelta(days=22)).date().isoformat()
    assert by_day[d1]["count"] == 1 and by_day[d1]["top"]["title"] == "Iran talks (TG)"
    assert by_day[d2]["count"] == 1 and by_day[d2]["top"]["title"] == "Tehran markets"
    assert by_day[d22]["count"] == 1
    assert sum(b["count"] for b in r["buckets"]) == 3
    assert len(r["spark"]) == 31


async def test_timeline_with_nothing_to_match_is_empty(session_factory):
    r = await timeline("!!!", days=10, sf=session_factory)
    assert r["modes"] == [] and r["buckets"] == [] and r["total"] == 0
