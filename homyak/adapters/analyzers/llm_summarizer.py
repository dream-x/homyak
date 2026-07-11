"""Analyzer stage 5: саммари 2-3 предложения через qwen. Best-effort."""

from __future__ import annotations

import structlog

from homyak.core.interfaces import AnalyzerContext
from homyak.core.llm import OllamaLLM

log = structlog.get_logger(__name__)

_SYSTEM = (
    "Суммируй новость в 2-3 предложениях на языке оригинала. "
    "Выведи только саммари, без преамбул и кавычек."
)


class LlmSummarizerAnalyzer:
    name = "llm_summarizer"
    stage = 5

    def __init__(self, llm: OllamaLLM | None = None) -> None:
        self._llm = llm or OllamaLLM()

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        if not (item.title or item.text):
            return
        user = f"{item.title or ''}\n\n{(item.text or '')[:2000]}"
        try:
            summary = await self._llm.chat_text(_SYSTEM, user)
        except Exception as e:  # best-effort
            log.warning("llm_summarizer_failed", item=ctx.item_id, error=str(e))
            return
        ctx.summary = summary.strip() or None
