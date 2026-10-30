"""split_cluster: ложно склеенный кластер разбирается по группам url_normalized (DB-тест)."""

from sqlalchemy import select

from homyak.core.interfaces import NewsItemDTO
from homyak.core.models import Cluster, NewsItem
from homyak.storage.postgres import NewsRepo


async def _glue(session_factory, ids: list[int]) -> int:
    """Как это сделал бы similarity_dedup: один кластер на всех."""
    async with session_factory() as s:
        cluster = Cluster(representative_id=ids[0], size=len(ids))
        s.add(cluster)
        await s.flush()
        for iid in ids:
            item = await s.get(NewsItem, iid)
            item.cluster_id = cluster.id
        await s.commit()
        return cluster.id


async def test_split_keeps_same_url_together_and_separates_the_rest(session_factory):
    repo = NewsRepo(session_factory)
    ids = []
    for sid, url in (
        ("a", "https://x.test/one?utm_source=tg"),
        ("b", "https://www.x.test/one/"),  # тот же url_normalized, что у a
        ("c", "https://x.test/two"),
        ("d", "https://x.test/three"),
    ):
        iid, _ = await repo.upsert_item(NewsItemDTO(source_type="rss", source_id=sid, url=url))
        ids.append(iid)
    glued = await _glue(session_factory, ids)

    result = await repo.split_cluster(glued)

    assert result == {"cluster_id": glued, "items": 4, "clusters": 3}
    async with session_factory() as s:
        rows = (
            await s.execute(select(NewsItem.id, NewsItem.cluster_id).order_by(NewsItem.id))
        ).all()
        by_id = dict(rows)
        assert by_id[ids[0]] == by_id[ids[1]] == glued  # первая группа остаётся под старым id
        assert len({by_id[ids[0]], by_id[ids[2]], by_id[ids[3]]}) == 3
        sizes = dict((await s.execute(select(Cluster.id, Cluster.size))).all())
        assert sizes[glued] == 2
        assert sizes[by_id[ids[2]]] == 1 and sizes[by_id[ids[3]]] == 1


async def test_split_of_unknown_cluster_is_a_noop(session_factory):
    repo = NewsRepo(session_factory)
    assert await repo.split_cluster(424242) == {"cluster_id": 424242, "items": 0, "clusters": 0}
