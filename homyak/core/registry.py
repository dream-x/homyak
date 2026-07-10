"""Регистратор плагинов: собирает включённые source'ы и упорядоченные analyzer'ы.

Ядро/pipeline обращаются только сюда — конкретные классы не хардкодятся в процессах.
"""

from __future__ import annotations

from homyak.adapters.analyzers.url_dedup import UrlDedupAnalyzer
from homyak.adapters.sources.miniflux import MinifluxSource
from homyak.adapters.sources.rss import RSSSource
from homyak.core.config import SourcesConfig
from homyak.core.interfaces import Analyzer, PollSource


def build_poll_sources(cfg: SourcesConfig) -> list[PollSource]:
    sources: list[PollSource] = []
    if cfg.miniflux and cfg.miniflux.enabled:
        sources.append(MinifluxSource(cfg.miniflux))
    for feed in cfg.rss:
        sources.append(RSSSource(feed))
    return sources


def build_analyzers() -> list[Analyzer]:
    """Список analyzer'ов, упорядоченный по stage. В Phase 2 — только url_dedup."""
    analyzers: list[Analyzer] = [UrlDedupAnalyzer()]
    return sorted(analyzers, key=lambda a: a.stage)
