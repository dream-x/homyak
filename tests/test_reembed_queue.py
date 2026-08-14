"""Очередь на переэмбеддинг: попадание в неё и выбор порции."""

from datetime import datetime, timezone

from homyak.core.config import settings
from homyak.core.interfaces import NewsItemDTO
from homyak.core.models import NewsItem
from homyak.storage.postgres import NewsRepo


_KEEP = object()  # «не задано» — иначе version=None не отличить от «оставь как есть»


async def _seed(session_factory, repo, source_id, *, model=_KEEP, version=_KEEP,
                processed=True) -> int:
    id_, _ = await repo.upsert_item(
        NewsItemDTO(source_type="rss", source_id=source_id, url=f"https://x.com/{source_id}",
                    title=source_id, text="исходный текст")
    )
    async with session_factory() as s:
        item = await s.get(NewsItem, id_)
        item.embedding_model = settings.embedding_model if model is _KEEP else model
        item.embedding_version = settings.embedding_version if version is _KEEP else version
        if processed:
            item.processed_at = datetime.now(timezone.utc)
        await s.commit()
    return id_


async def test_fresh_vectors_are_not_queued(session_factory):
    repo = NewsRepo(session_factory)
    await _seed(session_factory, repo, "ok")
    assert await NewsRepo(session_factory).stale_embedding_count() == 0


async def test_never_embedded_and_outdated_model_are_queued(session_factory):
    repo = NewsRepo(session_factory)
    never = await _seed(session_factory, repo, "never", version=None)
    old_model = await _seed(session_factory, repo, "old-model", model="prev-model")
    old_ver = await _seed(session_factory, repo, "old-version", version=settings.embedding_version - 1)
    await _seed(session_factory, repo, "ok")

    assert set(await NewsRepo(session_factory).stale_embedding_ids()) == {never, old_model, old_ver}


async def test_rewriting_text_queues_the_item(session_factory):
    """Главный инвариант: вектор построен по прежнему тексту и после перезаписи ищет не то.

    Именно этого не было, когда бэкфилл README переписал сотни записей — поиск продолжал
    искать их по прежним, коротким блёрбам.
    """
    repo = NewsRepo(session_factory)
    id_ = await _seed(session_factory, repo, "a")
    assert await NewsRepo(session_factory).stale_embedding_count() == 0

    await repo.set_item_text(id_, "полный README на много тысяч символов")
    assert await NewsRepo(session_factory).stale_embedding_ids() == [id_]


async def test_unprocessed_items_are_left_to_the_pipeline(session_factory):
    """У необработанных версия тоже NULL, но их эмбеддит процессор — и по полному тексту.

    Планировщик, взяв их, гонялся бы с ним наперегонки и жёг GPU на работу, которую пайплайн
    всё равно переделает: article_fetch дотягивает статью уже после того, как запись создана.
    """
    repo = NewsRepo(session_factory)
    await _seed(session_factory, repo, "ещё-в-очереди", version=None, processed=False)
    assert await NewsRepo(session_factory).stale_embedding_count() == 0


async def test_batch_takes_the_freshest_first(session_factory):
    """Порция ограничена, и брать надо свежее: архив ищут реже, чем вчерашнее."""
    repo = NewsRepo(session_factory)
    ids = [await _seed(session_factory, repo, f"i{i}", version=None) for i in range(5)]
    assert await NewsRepo(session_factory).stale_embedding_ids(limit=2) == sorted(ids, reverse=True)[:2]


async def test_top_tags_is_a_real_vocabulary(session_factory):
    """Словарь для рефайнмента: без него LLM сочиняет теги, которых теггер не ставит никогда."""
    from homyak.core.models import NewsItem as NI

    repo = NewsRepo(session_factory)
    for i, (tags, vertical) in enumerate(
        [(["rust", "systems"], "it"), (["rust"], "it"), (["pharma"], "medical")]
    ):
        id_ = await _seed(session_factory, repo, f"t{i}")
        async with session_factory() as s:
            item = await s.get(NI, id_)
            item.tags = tags
            item.vertical = vertical
            await s.commit()

    tags = await repo.top_tags("it")
    assert tags[0] == "rust"  # по убыванию частоты
    assert "systems" in tags
    assert "pharma" not in tags  # чужая вертикаль
