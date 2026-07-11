"""Регистратор плагинов: собирает включённые source'ы и упорядоченные analyzer'ы.

Ядро/pipeline обращаются только сюда — конкретные классы не хардкодятся в процессах.
"""

from __future__ import annotations

from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.adapters.analyzers.similarity_dedup import SimilarityDedupAnalyzer
from homyak.adapters.analyzers.url_dedup import UrlDedupAnalyzer
from homyak.adapters.sources.miniflux import MinifluxSource
from homyak.adapters.sources.rss import RSSSource
from homyak.core.config import SourcesConfig
from homyak.core.interfaces import Analyzer, PollSource
from homyak.storage.qdrant import QdrantStore


def build_poll_sources(cfg: SourcesConfig) -> list[PollSource]:
    sources: list[PollSource] = []
    if cfg.miniflux and cfg.miniflux.enabled:
        sources.append(MinifluxSource(cfg.miniflux))
    for feed in cfg.rss:
        sources.append(RSSSource(feed))
    return sources


def build_analyzers(qdrant: QdrantStore | None = None) -> list[Analyzer]:
    """Analyzer'ы, упорядоченные по stage.

    Без qdrant — только url_dedup (Phase 2 / тесты без векторной инфры). С qdrant добавляются
    embedder (stage 2) + similarity_dedup (stage 3).
    """
    analyzers: list[Analyzer] = [UrlDedupAnalyzer()]
    if qdrant is not None:
        analyzers.append(EmbedderAnalyzer(qdrant))
        analyzers.append(SimilarityDedupAnalyzer(qdrant))
    return sorted(analyzers, key=lambda a: a.stage)
