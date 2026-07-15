"""Гейт предобработки: что режем, а что пропускаем безусловно."""

from dataclasses import dataclass

import pytest

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
