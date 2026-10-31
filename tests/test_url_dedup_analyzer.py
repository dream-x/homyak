from homyak.adapters.analyzers.url_dedup import UrlDedupAnalyzer
from homyak.core.interfaces import AnalyzerContext, NewsItemDTO
from homyak.core.models import NewsItem
from homyak.storage.postgres import NewsRepo


async def _process(session_factory, repo, item_id):
    """Мимикрия processor'а: url_dedup + persist cluster_id (как mark_processed в бою)."""
    analyzer = UrlDedupAnalyzer()
    async with session_factory() as s:
        item = await s.get(NewsItem, item_id)
        ctx = AnalyzerContext(item_id=item_id, item=item, session=s)
        await analyzer.analyze(ctx)
        await s.commit()
    await repo.mark_processed(item_id, cluster_id=ctx.cluster_id)
    return ctx.cluster_id


async def test_same_normalized_url_merges_into_one_cluster(session_factory):
    repo = NewsRepo(session_factory)
    id1, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="a", url="https://x.com/p?utm_source=1")
    )
    id2, _ = await repo.upsert_item(
        NewsItemDTO(source_type="telegram", source_id="b", url="https://www.x.com/p/")
    )
    c1 = await _process(session_factory, repo, id1)
    c2 = await _process(session_factory, repo, id2)
    assert c1 == c2  # разные источники, один URL → один кластер


async def test_different_urls_get_different_clusters(session_factory):
    repo = NewsRepo(session_factory)
    id1, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="a", url="https://x.com/one")
    )
    id2, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="b", url="https://x.com/two")
    )
    c1 = await _process(session_factory, repo, id1)
    c2 = await _process(session_factory, repo, id2)
    assert c1 != c2



async def test_a_rerun_keeps_the_existing_cluster(session_factory):
    """Повторная обработка (resummarize, сброс processed_at) не выдёргивает item в свежий кластер."""
    repo = NewsRepo(session_factory)
    id1, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="a", url="https://x.com/one")
    )
    c1 = await _process(session_factory, repo, id1)
    assert await _process(session_factory, repo, id1) == c1


async def test_joining_by_url_only_decides_and_marks_the_context(session_factory):
    """Счётчик и перепроверка близнеца — в similarity_dedup под локом; здесь только решение."""
    from sqlalchemy import select

    from homyak.core.models import Cluster

    repo = NewsRepo(session_factory)
    id1, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id="a", url="https://x.com/same")
    )
    id2, _ = await repo.upsert_item(
        NewsItemDTO(source_type="telegram", source_id="b", url="https://x.com/same")
    )
    c1 = await _process(session_factory, repo, id1)
    async with session_factory() as s:
        item = await s.get(NewsItem, id2)
        ctx = AnalyzerContext(item_id=id2, item=item, session=s)
        await UrlDedupAnalyzer().analyze(ctx)
        assert ctx.cluster_joined and ctx.cluster_id == c1 and item.cluster_id == c1
        await s.commit()
    async with session_factory() as s:
        assert await s.scalar(select(Cluster.size).where(Cluster.id == c1)) == 1
