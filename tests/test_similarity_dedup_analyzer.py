"""Мерж similarity_dedup против живой БД (homyak_test) с подменённым Qdrant.

Проверяем то, что чистые within_window/cluster_full не видят: потолок считается по РЕЗУЛЬТАТУ
слияния (цель + переносимые + текущий), по фактическим членам, а не по Cluster.size; членство
текущего item'а и перенос старого кластера — одна транзакция; хит, уехавший в другой кластер
между чтением и локом, мерж отменяет.
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete, select, text, update

from homyak.adapters.analyzers.similarity_dedup import SimilarityDedupAnalyzer
from homyak.adapters.analyzers.url_dedup import UrlDedupAnalyzer
from homyak.core.interfaces import AnalyzerContext, NewsItemDTO
from homyak.core.models import Cluster, NewsItem
from homyak.storage.postgres import NewsRepo

_NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


class _Qdrant:
    def __init__(self, hits):
        self.hits = hits

    async def search_similar(self, vector, limit, score_threshold, exclude_id):
        return self.hits


async def _items(repo, n: int, prefix: str, url: str | None = None) -> list[int]:
    ids = []
    for i in range(n):
        iid, _ = await repo.upsert_item(
            NewsItemDTO(
                source_type="rss",
                source_id=f"{prefix}{i}",
                url=url or f"https://x.test/{prefix}/{i}",
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


# --- присоединение по URL финализируется здесь, под тем же локом, что и мерж/split ---


async def _stage1(s, item_id: int) -> AnalyzerContext:
    item = await s.get(NewsItem, item_id)
    ctx = AnalyzerContext(item_id=item_id, item=item, session=s)
    ctx.embedding = [0.1] * 4
    await UrlDedupAnalyzer().analyze(ctx)
    return ctx


async def test_url_join_writes_membership_and_exact_size(session_factory):
    repo = NewsRepo(session_factory)
    (twin,) = await _items(repo, 1, "t", url="https://x.test/same")
    (new_id,) = await _items(repo, 1, "n", url="https://x.test/same?utm_source=rss")
    target = await _cluster(session_factory, [twin], size=7)  # size врёт
    analyzer = SimilarityDedupAnalyzer(_Qdrant([]), max_age_hours=0)

    async with session_factory() as s:
        ctx = await _stage1(s, new_id)
        assert ctx.cluster_joined and ctx.cluster_id == target
        await analyzer.analyze(ctx)
        await s.commit()

    assert await _members(session_factory, target) == sorted([twin, new_id])
    assert await _size(session_factory, target) == 2


async def test_split_between_url_join_and_the_lock_detaches_the_item(session_factory):
    """Две настоящие транзакции: стадия 1 присоединила item к C по близнецу, split(C) увёл
    близнеца в D раньше, чем стадия 3 взяла лок. Item не должен остаться в C без близнеца и не
    должен лезть в D под чужой лок — он уходит в свой свежий кластер."""
    repo = NewsRepo(session_factory)
    (x,) = await _items(repo, 1, "x", url="https://x.test/first")
    (twin,) = await _items(repo, 1, "t", url="https://x.test/same")
    (new_id,) = await _items(repo, 1, "n", url="https://x.test/same")
    c = await _cluster(session_factory, [x, twin])  # группа x (меньший id) остаётся в C
    analyzer = SimilarityDedupAnalyzer(_Qdrant([]), max_age_hours=0)

    async with session_factory() as s:
        ctx = await _stage1(s, new_id)
        assert ctx.cluster_id == c
        split = await repo.split_cluster(c)  # другая сессия, коммитит сама
        assert split["clusters"] == 2
        await analyzer.analyze(ctx)
        await s.commit()
        fresh = ctx.cluster_id

    d = (await _cluster_of(session_factory, twin))
    assert d != c
    assert fresh not in (c, d)
    assert await _members(session_factory, c) == [x]
    assert await _members(session_factory, d) == [twin]
    assert await _members(session_factory, fresh) == [new_id]
    assert (await _size(session_factory, c), await _size(session_factory, d), await _size(session_factory, fresh)) == (1, 1, 1)


async def test_mark_processed_does_not_undo_a_split(session_factory):
    """Коммит стадий → split → mark_processed со старым cluster_id: запись остаётся там, куда её
    положил split; пустое членство fallback по-прежнему заполняет."""
    repo = NewsRepo(session_factory)
    (x,) = await _items(repo, 1, "x", url="https://x.test/first")
    (item,) = await _items(repo, 1, "i", url="https://x.test/second")
    (orphan,) = await _items(repo, 1, "o")
    c = await _cluster(session_factory, [x, item])

    await repo.split_cluster(c)
    d = await _cluster_of(session_factory, item)
    assert d != c

    await repo.mark_processed(item, cluster_id=c)
    assert await _cluster_of(session_factory, item) == d
    await repo.mark_processed(orphan, cluster_id=c)
    assert await _cluster_of(session_factory, orphan) == c


async def _cluster_of(session_factory, item_id: int) -> int | None:
    async with session_factory() as s:
        return await s.scalar(select(NewsItem.cluster_id).where(NewsItem.id == item_id))


async def test_a_concurrent_merge_may_delete_the_joined_cluster_before_our_lock(session_factory):
    """Стадия 1 выбрала C по близнецу, тело item'а уже сброшено в БД (autoflush), а встречный мерж
    в другой транзакции уносит C в E и УДАЛЯЕТ C. Раньше FK item→C, поставленный до лока, держал
    строку C (KEY SHARE) — мерж ждал нас, мы — его advisory-лок. Теперь мерж проходит сразу, а мы
    под локом видим, что близнец уехал, и сами уходим в E по вектору."""
    repo = NewsRepo(session_factory)
    (twin,) = await _items(repo, 1, "t", url="https://x.test/same")
    (e0,) = await _items(repo, 1, "e")
    (new_id,) = await _items(repo, 1, "n", url="https://x.test/same")
    c = await _cluster(session_factory, [twin])
    e = await _cluster(session_factory, [e0])
    analyzer = SimilarityDedupAnalyzer(_Qdrant([(e0, 0.95, {})]), max_age_hours=0)

    async def merge_c_into_e():
        async with session_factory() as b:
            for k in sorted((c, e)):
                await b.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": k})
            await b.execute(update(NewsItem).where(NewsItem.cluster_id == c).values(cluster_id=e))
            await b.execute(delete(Cluster).where(Cluster.id == c))
            await b.execute(update(Cluster).where(Cluster.id == e).values(size=2))
            await b.commit()

    async with session_factory() as s:
        ctx = await _stage1(s, new_id)
        ctx.item.title = "flushed body"
        await s.flush()  # как autoflush перед SELECT в _pick_target
        await asyncio.wait_for(merge_c_into_e(), 5)  # не должен ждать нашу транзакцию
        await analyzer.analyze(ctx)
        await s.commit()
        assert ctx.cluster_id == e

    assert await _members(session_factory, e) == sorted([twin, e0, new_id])
    assert await _size(session_factory, e) == 3
    assert await _size(session_factory, c) is None


async def test_a_rerun_leaves_membership_alone_even_with_a_similar_hit(session_factory):
    """Повторная обработка уже кластеризованной записи: split между чтением и стадией 3 увёл её
    в D, вектор зовёт в E. Ни группа, оставшаяся в C, ни сама запись никуда не едут."""
    repo = NewsRepo(session_factory)
    (x,) = await _items(repo, 1, "x", url="https://x.test/first")
    (item,) = await _items(repo, 1, "i", url="https://x.test/second")
    (e0,) = await _items(repo, 1, "e")
    c = await _cluster(session_factory, [x, item])
    e = await _cluster(session_factory, [e0])
    analyzer = SimilarityDedupAnalyzer(_Qdrant([(e0, 0.95, {})]), max_age_hours=0)

    async with session_factory() as s:
        ctx = await _stage1(s, item)
        assert ctx.cluster_kept and ctx.cluster_id == c
        await repo.split_cluster(c)
        await analyzer.analyze(ctx)
        await s.commit()

    d = await _cluster_of(session_factory, item)
    assert d not in (c, e)
    assert await _members(session_factory, c) == [x]
    assert await _members(session_factory, d) == [item]
    assert await _members(session_factory, e) == [e0]
