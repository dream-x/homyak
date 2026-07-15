"""Гейт предобработки: что режем, а что пропускаем безусловно."""

from dataclasses import dataclass


from homyak.adapters.analyzers.prefilter import PrefilterAnalyzer
from homyak.core.config import settings


@dataclass
class _Item:
    watch_topics: list
    skip_reason: str | None = None


@dataclass
class _Ctx:
    item: _Item
    embedding: list | None
    session: object = None
    skip: bool = False
    skip_reason: str | None = None


def _gate(refs):
    g = PrefilterAnalyzer(qdrant=None)
    g._refs = refs  # опорные вектора профилей — подставляем напрямую
    return g


ALIGNED = [1.0, 0.0]  # совпадает с профилем → cos = 1.0
ORTHOGONAL = [0.0, 1.0]  # перпендикулярен → cos = 0.0
REFS = {"it": [1.0, 0.0]}


async def test_junk_far_from_profiles_is_skipped():
    g = _gate(REFS)
    ctx = _Ctx(item=_Item(watch_topics=[]), embedding=ORTHOGONAL)
    await g.analyze(ctx)
    assert ctx.skip is True
    assert "близость" in ctx.skip_reason


async def test_relevant_item_passes():
    g = _gate(REFS)
    ctx = _Ctx(item=_Item(watch_topics=[]), embedding=ALIGNED)
    await g.analyze(ctx)
    assert ctx.skip is False


async def test_watchlist_always_passes_even_if_far():
    """Ключевая защита: твит про Иран дал близость 0.372 — семантика бы выкинула, а это watchlist."""
    g = _gate(REFS)
    ctx = _Ctx(item=_Item(watch_topics=["Iran"]), embedding=ORTHOGONAL)
    await g.analyze(ctx)
    assert ctx.skip is False


async def test_no_embedding_passes():
    g = _gate(REFS)
    ctx = _Ctx(item=_Item(watch_topics=[]), embedding=None)
    await g.analyze(ctx)
    assert ctx.skip is False  # судить не на чем → лучше пропустить, чем потерять


async def test_no_profiles_passes():
    g = _gate({})
    ctx = _Ctx(item=_Item(watch_topics=[]), embedding=ORTHOGONAL)
    await g.analyze(ctx)
    assert ctx.skip is False


async def test_disabled_gate_passes_everything(monkeypatch):
    monkeypatch.setattr(settings, "prefilter_enabled", False)
    g = _gate(REFS)
    ctx = _Ctx(item=_Item(watch_topics=[]), embedding=ORTHOGONAL)
    await g.analyze(ctx)
    assert ctx.skip is False


# --- регрессы на дефекты, найденные аудитом ---


class _FakeSession:
    """Сессия, которая падает N раз, потом отдаёт профиль."""

    def __init__(self, fail_times=0, version=1):
        self.fail_times = fail_times
        self.version = version
        self.calls = 0

    async def execute(self, *a, **k):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("ollama/db моргнули")

        class _R:
            def __init__(s, v):
                s.v = v

            def all(s):
                return [("it", s.v, "профиль", "[]")]

        return _R(self.version)


class _FakeEmb:
    async def _embed(self, text):
        return [1.0, 0.0]


async def test_transient_failure_does_not_kill_gate_forever():
    """Регресс: self._refs={} при ошибке выключал гейт НАВСЕГДА до рестарта процесса."""
    import homyak.adapters.analyzers.prefilter as pf

    g = pf.PrefilterAnalyzer(qdrant=None, embedder=_FakeEmb())
    s = _FakeSession(fail_times=1)

    refs = await g._refs_vectors(s)  # первый вызов падает
    assert refs == {}  # fail-open: гейт не режет

    g._refs_at = 0.0  # эмулируем истёкший retry-бэкофф
    refs = await g._refs_vectors(s)  # второй — должен ПОВТОРИТЬ, а не вернуть кэш провала
    assert "it" in refs, "гейт остался мёртвым после транзиентной ошибки"


async def test_profile_change_invalidates_refs():
    """Регресс: гейт судил по протухшему вектору → новости молча терялись безвозвратно."""
    import homyak.adapters.analyzers.prefilter as pf

    g = pf.PrefilterAnalyzer(qdrant=None, embedder=_FakeEmb())
    s = _FakeSession(version=1)

    await g._refs_vectors(s)
    assert g._sig == (("it", 1),)

    s.version = 2  # пользователь нажал 🔇 → set_profile → новая версия
    g._refs_at = 0.0  # TTL истёк
    await g._refs_vectors(s)
    assert g._sig == (("it", 2),), "кэш не инвалидировался при смене версии профиля"


async def test_refs_cache_holds_within_ttl():
    """Кэш не должен долбить БД на каждый айтем."""
    import homyak.adapters.analyzers.prefilter as pf

    g = pf.PrefilterAnalyzer(qdrant=None, embedder=_FakeEmb())
    s = _FakeSession(version=1)
    await g._refs_vectors(s)
    before = s.calls
    await g._refs_vectors(s)  # сразу же — должен отдать кэш
    assert s.calls == before, "кэш не работает — лишний запрос на каждый айтем"
