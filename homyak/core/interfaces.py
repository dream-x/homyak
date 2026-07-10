"""Контракты плагинной системы: sources, analyzers, outputs.

Ядро оперирует только этими Protocol'ами и DTO — оно не знает о конкретных источниках/
анализаторах/выходах. Analyzer'ы мутируют общий `AnalyzerContext` (не возвращают результат),
чтобы стадии дёшево делились уже посчитанным (embedding, cluster_id, ...).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from homyak.core.models import NewsItem


@dataclass(slots=True)
class NewsItemDTO:
    """То, что выдаёт source-адаптер. Без сайд-эффектов — upsert делает runner."""

    source_type: str
    source_id: str
    id: int | None = None  # присваивается после upsert; None на входе от source-адаптера
    url: str | None = None
    title: str | None = None
    text: str | None = None
    media: list[str] = field(default_factory=list)
    author: str | None = None
    raw_score: float | None = None
    published_at: datetime | None = None
    category: str | None = None
    # заполняется обработкой:
    tags: list[str] | None = None
    cluster_id: int | None = None


@runtime_checkable
class PollSource(Protocol):
    """Периодически опрашиваемый источник (RSS, Miniflux). Ядро крутит по interval_seconds."""

    name: str
    interval_seconds: int

    def poll(self, cursor: str | None) -> AsyncIterator[tuple[NewsItemDTO, str]]: ...


@runtime_checkable
class PushSource(Protocol):
    """Long-running источник с подпиской (telegram-relay). Ядро запускает как task."""

    name: str

    async def subscribe(self, sink: Callable[[NewsItemDTO], Awaitable[None]]) -> None: ...


@dataclass
class AnalyzerContext:
    """Разделяемый между стадиями контекст обработки одного item'а. Analyzer'ы его мутируют."""

    item_id: int
    item: "NewsItem"
    session: "AsyncSession"
    # прокидывается между стадиями:
    embedding: list[float] | None = None
    cluster_id: int | None = None
    tags: list[str] | None = None
    summary: str | None = None
    score: float | None = None
    llm_relevance: float | None = None
    llm_reason: str | None = None
    personal_score: float | None = None


@runtime_checkable
class Analyzer(Protocol):
    """Стадия обработки. Мутирует ctx, ничего не возвращает. Порядок — по `stage`."""

    name: str
    stage: int

    async def analyze(self, ctx: AnalyzerContext) -> None: ...


@dataclass(slots=True)
class FeedQuery:
    category: str | None = None
    source_types: list[str] | None = None
    min_score: float | None = None
    since: datetime | None = None
    collapse_clusters: bool = True
    limit: int = 100
    cursor: str | None = None


@dataclass(slots=True)
class Feed:
    items: list[NewsItemDTO]
    next_cursor: str | None = None


@runtime_checkable
class OutputAdapter(Protocol):
    name: str

    async def serve(self, query: FeedQuery, session: "AsyncSession") -> Feed: ...
