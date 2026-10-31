"""Пульс против живой БД: ширина истории = независимые ленты, вотчлист против базы 7 окон."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from homyak.core.interfaces import NewsItemDTO
from homyak.core.models import Cluster, NewsItem
from homyak.core.pulse import pulse
from homyak.storage.postgres import NewsRepo

_NOW = datetime.now(timezone.utc)


async def _item(session_factory, repo, sid, *, hours_ago, feed, cluster=None, score=0.5,
                watch=(), vertical="business"):
    iid, _ = await repo.upsert_item(
        NewsItemDTO(
            source_type="rss", source_id=sid, url=f"https://x.test/{sid}", feed_name=feed,
            published_at=_NOW - timedelta(hours=hours_ago), title=f"story {sid}",
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
                cluster_id=cluster, vertical=vertical, processed_at=_NOW, personal_score=score,
                watch_topics=list(watch),
            )
        )
        await s.commit()
    return iid, cluster


async def test_pulse_ranks_by_independent_feeds_and_tracks_the_watchlist(session_factory, monkeypatch):
    from homyak.core import pulse as pl

    monkeypatch.setattr(pl, "watch_topic_names", lambda: ["Iran", "Oil"])
    repo = NewsRepo(session_factory)
    # история A: три ленты, 4 записи; история B: одна лента, 6 записей; C: одиночка с высоким скором
    _, a = await _item(session_factory, repo, "a1", hours_ago=1, feed="reuters", score=0.4, watch=["Iran"])
    await _item(session_factory, repo, "a2", hours_ago=2, feed="bloomberg", cluster=a, score=0.7, watch=["Iran"])
    await _item(session_factory, repo, "a3", hours_ago=3, feed="@tg_channel", cluster=a, score=0.5)
    await _item(session_factory, repo, "a4", hours_ago=3, feed="reuters", cluster=a, score=0.5)
    _, b = await _item(session_factory, repo, "b1", hours_ago=1, feed="spam", score=0.9)
    for i in range(2, 7):
        await _item(session_factory, repo, f"b{i}", hours_ago=2, feed="spam", cluster=b, score=0.9)
    await _item(session_factory, repo, "c", hours_ago=5, feed="ft", score=0.95, watch=["Oil"])
    # база вотчлиста: Iran — 7 историй за прошлые 7 окон (по одной в окно), Oil — ни одной
    for i in range(7):
        await _item(session_factory, repo, f"old{i}", hours_ago=30 + 24 * i, feed="ft", watch=["Iran"])
    await _item(session_factory, repo, "it", hours_ago=1, feed="hn", vertical="it", watch=["Oil"])  # чужая вертикаль

    r = await pulse(24, "business", limit=5, sf=session_factory)

    assert [s["title"] for s in r["stories"]] == ["story a2", "story b1", "story c"]
    assert r["stories"][0]["feeds"] == 3 and r["stories"][0]["items"] == 4 and r["stories"][0]["wide"]
    assert r["stories"][1]["feeds"] == 1 and r["stories"][1]["items"] == 6
    watch = {w["name"]: w for w in r["watch"]}
    assert watch["Iran"]["count"] == 1 and watch["Iran"]["base"] == 1.0 and watch["Iran"]["direction"] == "→"
    assert watch["Oil"]["count"] == 1 and watch["Oil"]["base"] == 0.0  # IT-запись не считается
    assert r["vertical"] == "business" and r["hours"] == 24
