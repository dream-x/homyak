"""Ссылка, принесённая человеком через бота: она не должна теряться по дороге."""

from homyak.adapters.analyzers.prefilter import PrefilterAnalyzer
from homyak.core.interfaces import AnalyzerContext, NewsItemDTO
from homyak.core.models import NewsItem
from homyak.storage.postgres import NewsRepo


class _StubEmbedder:
    async def _embed(self, text):
        return [1.0, 0.0, 0.0]


async def _seed(session_factory, source_type="rss", embedding=None):
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(
        NewsItemDTO(
            source_type=source_type,
            source_id=f"{source_type}-1",
            url="https://example.com/a",
            title="A",
            text="текст" * 50,
        )
    )
    async with session_factory() as s:
        item = await s.get(NewsItem, id_)
        ctx = AnalyzerContext(item_id=id_, item=item, session=s)
        ctx.embedding = embedding or [0.0, 1.0, 0.0]  # ортогонален профилю → близость 0
        yield ctx


async def test_gate_drops_an_unrelated_rss_item(session_factory, monkeypatch):
    """Контроль: обычная запись, далёкая от профилей, гейтом отсеивается."""
    gate = PrefilterAnalyzer(qdrant=None, embedder=_StubEmbedder())
    monkeypatch.setattr(gate, "_refs_vectors", lambda s: _refs())

    async for ctx in _seed(session_factory, "rss"):
        await gate.analyze(ctx)
        assert ctx.skip is True


async def test_gate_never_drops_a_hand_added_link(session_factory, monkeypatch):
    """Отсев необратим — пути переобработки skipped нет, а ссылку выбрал человек."""
    gate = PrefilterAnalyzer(qdrant=None, embedder=_StubEmbedder())
    monkeypatch.setattr(gate, "_refs_vectors", lambda s: _refs())

    async for ctx in _seed(session_factory, "manual"):
        await gate.analyze(ctx)
        assert ctx.skip is False


async def _refs():
    return {"it": [1.0, 0.0, 0.0]}


async def test_star_is_recorded_once_per_item(session_factory):
    """Повторная доставка processed не должна ставить вторую звезду и слать второй пост."""
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(
        NewsItemDTO(source_type="manual", source_id="m-1", url="https://example.com/x", title="X")
    )
    assert (await repo.record_feedback(id_, "save", None))[0] == "added"
    assert (await repo.record_feedback(id_, "save", None))[0] == "removed"  # toggle
    assert (await repo.record_feedback(id_, "save", None))[0] == "added"


async def test_manual_link_is_deduped_by_normalized_url(session_factory):
    """Одна и та же ссылка с utm-хвостом — одна запись, а не вторая карточка в канале."""
    repo = NewsRepo(session_factory)
    from homyak.core.urls import normalize_url

    first, new1 = await repo.upsert_item(
        NewsItemDTO(source_type="manual", source_id=normalize_url("https://example.com/p"),
                    url="https://example.com/p", title=None)
    )
    second, new2 = await repo.upsert_item(
        NewsItemDTO(source_type="manual",
                    source_id=normalize_url("https://www.example.com/p/?utm_source=tg"),
                    url="https://example.com/p", title=None)
    )
    assert new1 is True
    assert second == first and new2 is False
