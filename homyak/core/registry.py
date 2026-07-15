"""Регистратор плагинов: собирает включённые source'ы и упорядоченные analyzer'ы.

Ядро/pipeline обращаются только сюда — конкретные классы не хардкодятся в процессах.
"""

from __future__ import annotations

from homyak.adapters.analyzers.article_fetch import ArticleFetchAnalyzer
from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.adapters.analyzers.llm_relevance import LlmRelevanceAnalyzer
from homyak.adapters.analyzers.llm_summarizer import LlmSummarizerAnalyzer
from homyak.adapters.analyzers.llm_tagger import LlmTaggerAnalyzer
from homyak.adapters.analyzers.personalizer import PersonalizerAnalyzer
from homyak.adapters.analyzers.prefilter import PrefilterAnalyzer
from homyak.adapters.analyzers.scorer import ScorerAnalyzer
from homyak.adapters.analyzers.similarity_dedup import SimilarityDedupAnalyzer
from homyak.adapters.analyzers.url_dedup import UrlDedupAnalyzer
from homyak.adapters.analyzers.watchlist_matcher import WatchlistAnalyzer
from homyak.adapters.sources.miniflux import MinifluxSource
from homyak.adapters.sources.rss import RSSSource
from homyak.core.config import SourcesConfig
from homyak.core.interfaces import Analyzer, PollSource
from homyak.core.llm import OllamaLLM
from homyak.storage.qdrant import QdrantStore


def build_poll_sources(cfg: SourcesConfig) -> list[PollSource]:
    sources: list[PollSource] = []
    if cfg.miniflux and cfg.miniflux.enabled:
        sources.append(MinifluxSource(cfg.miniflux))
    for feed in cfg.rss:
        sources.append(RSSSource(feed))
    return sources


def build_analyzers(
    qdrant: QdrantStore | None = None, repo=None, with_llm: bool = True
) -> list[Analyzer]:
    """Analyzer'ы, упорядоченные по stage.

    Без qdrant — только url_dedup (Phase 2 / тесты). С qdrant: embedder (2), similarity_dedup (3),
    scorer (6). При with_llm: llm_tagger (4), llm_summarizer (5). При with_llm+repo (Phase 6):
    llm_relevance (7, судья) + personalizer (8, свёртка personal_score).
    """
    analyzers: list[Analyzer] = [ArticleFetchAnalyzer(), UrlDedupAnalyzer(), WatchlistAnalyzer()]
    if qdrant is not None:
        analyzers.append(EmbedderAnalyzer(qdrant))
        analyzers.append(SimilarityDedupAnalyzer(qdrant))
        analyzers.append(PrefilterAnalyzer(qdrant))  # гейт: режет шум до LLM
        if with_llm:
            llm = OllamaLLM()
            analyzers.append(LlmTaggerAnalyzer(llm))
            analyzers.append(LlmSummarizerAnalyzer())  # своя модель (SUMMARY_MODEL, побольше)
        analyzers.append(ScorerAnalyzer())
        if with_llm and repo is not None:
            analyzers.append(LlmRelevanceAnalyzer(repo))
            analyzers.append(PersonalizerAnalyzer(repo, qdrant))
    return sorted(analyzers, key=lambda a: a.stage)
