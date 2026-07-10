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
