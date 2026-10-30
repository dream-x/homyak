"""Мерж similarity_dedup против живой БД (homyak_test) с подменённым Qdrant.

Проверяем то, что чистые within_window/cluster_full не видят: потолок считается по РЕЗУЛЬТАТУ
слияния (цель + переносимые + текущий), по фактическим членам, а не по Cluster.size; членство
текущего item'а и перенос старого кластера — одна транзакция; хит, уехавший в другой кластер
между чтением и локом, мерж отменяет.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from homyak.adapters.analyzers.similarity_dedup import SimilarityDedupAnalyzer
from homyak.core.interfaces import AnalyzerContext, NewsItemDTO
from homyak.core.models import Cluster, NewsItem
from homyak.storage.postgres import NewsRepo

_NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


class _Qdrant:
    def __init__(self, hits):
        self.hits = hits

    async def search_similar(self, vector, limit, score_threshold, exclude_id):
        return self.hits


async def _items(repo, n: int, prefix: str) -> list[int]:
    ids = []
    for i in range(n):
        iid, _ = await repo.upsert_item(
            NewsItemDTO(
                source_type="rss",
                source_id=f"{prefix}{i}",
                url=f"https://x.test/{prefix}/{i}",
                published_at=_NOW,
            )
        )
        ids.append(iid)
    return ids


async def _cluster(session_factory, ids: list[int], *, size: int | None = None) -> int:
    """Кластер с заданными членами; size можно задать «вразнобой» с фактом — как в проде."""
    async with session_factory() as s:
        c = Cluster(representative_id=ids[0], size=size if size is not None else len(ids))
        s.add(c)
        await s.flush()
        for iid in ids:
            (await s.get(NewsItem, iid)).cluster_id = c.id
        await s.commit()
        return c.id


async def _run(session_factory, analyzer, item_id: int, cluster_id: int | None):
    async with session_factory() as s:
        item = await s.get(NewsItem, item_id)
        ctx = AnalyzerContext(item_id=item_id, item=item, session=s)
        ctx.cluster_id = cluster_id  # как после url_dedup
        ctx.embedding = [0.1] * 4
        await analyzer.analyze(ctx)
        await s.commit()
        return ctx.cluster_id


async def _members(session_factory, cluster_id: int) -> list[int]:
    async with session_factory() as s:
        rows = await s.execute(
            select(NewsItem.id).where(NewsItem.cluster_id == cluster_id).order_by(NewsItem.id)
        )
        return [r[0] for r in rows]


async def _size(session_factory, cluster_id: int) -> int | None:
    async with session_factory() as s:
        return await s.scalar(select(Cluster.size).where(Cluster.id == cluster_id))


async def test_merge_moves_the_old_cluster_and_writes_exact_size(session_factory):
    repo = NewsRepo(session_factory)
    target_ids = await _items(repo, 3, "t")
    old_ids = await _items(repo, 2, "o")
    (new_id,) = await _items(repo, 1, "n")
    target = await _cluster(session_factory, target_ids, size=1)  # size врёт, факт — 3
    old = await _cluster(session_factory, old_ids + [new_id])
    analyzer = SimilarityDedupAnalyzer(
        _Qdrant([(target_ids[0], 0.95, {"published_at": _NOW.isoformat()})]),
        max_cluster=60,
        max_age_hours=72,
    )

    assert await _run(session_factory, analyzer, new_id, old) == target

    assert await _members(session_factory, target) == sorted(target_ids + old_ids + [new_id])
    assert await _size(session_factory, target) == 6  # пересчитан по факту, а не 1 + 1 + moved
    assert await _size(session_factory, old) is None  # старый кластер удалён


async def test_cap_is_checked_on_the_merge_result(session_factory):
    """Цель 58 < 60, но вместе с переносимыми 3 и текущим — 62: мержа быть не должно."""
    repo = NewsRepo(session_factory)
    target_ids = await _items(repo, 58, "t")
    old_ids = await _items(repo, 3, "o")
    (new_id,) = await _items(repo, 1, "n")
    target = await _cluster(session_factory, target_ids)
    old = await _cluster(session_factory, old_ids + [new_id])
    analyzer = SimilarityDedupAnalyzer(
        _Qdrant([(target_ids[0], 0.95, {})]), max_cluster=60, max_age_hours=0
    )

    assert await _run(session_factory, analyzer, new_id, old) == old

    assert await _members(session_factory, target) == sorted(target_ids)
    assert await _members(session_factory, old) == sorted(old_ids + [new_id])
    assert await _size(session_factory, target) == 58


async def test_hit_that_moved_before_the_lock_cancels_the_merge(session_factory, monkeypatch):
    repo = NewsRepo(session_factory)
    target_ids = await _items(repo, 2, "t")
    (new_id,) = await _items(repo, 1, "n")
    target = await _cluster(session_factory, target_ids)
    old = await _cluster(session_factory, [new_id])
    analyzer = SimilarityDedupAnalyzer(_Qdrant([(target_ids[0], 0.95, {})]), max_age_hours=0)

    async def moved_elsewhere(s, item_id):  # split увёл хит в другой кластер между чтением и локом
        return target + 1000

    monkeypatch.setattr(analyzer, "_cluster_of", moved_elsewhere)

    assert await _run(session_factory, analyzer, new_id, old) == old
    assert await _members(session_factory, target) == sorted(target_ids)
    assert await _size(session_factory, old) == 1


async def test_old_hit_is_ignored(session_factory):
    repo = NewsRepo(session_factory)
    target_ids = await _items(repo, 1, "t")
    (new_id,) = await _items(repo, 1, "n")
    target = await _cluster(session_factory, target_ids)
    old = await _cluster(session_factory, [new_id])
    stale = datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat()
    analyzer = SimilarityDedupAnalyzer(
        _Qdrant([(target_ids[0], 0.95, {"published_at": stale})]), max_age_hours=72
    )

    assert await _run(session_factory, analyzer, new_id, old) == old
    assert await _members(session_factory, target) == target_ids
