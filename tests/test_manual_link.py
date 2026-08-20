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


async def test_auto_star_does_not_toggle_itself_off(session_factory):
    """Звезду ставит автомат, а record_feedback — переключатель.

    На повторной доставке processed (JetStream её допускает) наивный вызов снял бы звезду.
    has_feedback отвечает на вопрос «уже стоит?», ничего не меняя.
    """
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(
        NewsItemDTO(source_type="manual", source_id="m-2", url="https://example.com/y", title="Y")
    )
    assert await repo.has_feedback(id_, "save") is False
    await repo.record_feedback(id_, "save", None)
    assert await repo.has_feedback(id_, "save") is True
    assert await repo.has_feedback(id_, "up") is False  # другой сигнал не путаем
    assert await repo.has_feedback(id_, "save") is True  # проверка не мутирует


async def test_manual_item_is_marked_pushed_so_it_is_not_sent_twice(session_factory):
    """В цикле push_loop за _star_manual сразу идёт _maybe_push.

    Тот отбирает записи по `pushed_at IS NULL` и скору выше порога — то есть ручная ссылка
    прилетала в личку вторым сообщением сразу за карточкой. Отметка pushed это закрывает.
    """
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(
        NewsItemDTO(source_type="manual", source_id="m-3", url="https://example.com/z", title="Z")
    )
    from homyak.core.models import NewsItem as NI

    async with session_factory() as s:
        item = await s.get(NI, id_)
        item.personal_score = 0.9  # заведомо выше любого порога
        await s.commit()

    await repo.mark_pushed(id_)
    async with session_factory() as s:
        assert (await s.get(NI, id_)).pushed_at is not None
