"""Analyzer stage 5: качественное саммари через большую модель (SUMMARY_MODEL). Best-effort."""

from __future__ import annotations

import re

import structlog

from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.core.llm import OllamaLLM

log = structlog.get_logger(__name__)

_SYSTEM = (
    "Summarize the article for quick scanning, ENTIRELY in the article's original language "
    "(Russian article -> Russian summary, English -> English). "
    "First, 1-2 lively sentences: what it is about and why it's worth the reader's time, "
    "as if telling a colleague. Then, on new lines, 2-3 short takeaways, each starting with '• ', "
    "stating concretely what the reader will learn or take away. "
    "Strictly from the text, no speculation. No preamble, no quotes, no meta-commentary, no hashtags."
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _clean(text: str) -> str:
    text = _THINK_RE.sub("", text)  # у thinking-моделей вырезаем reasoning, если просочился
    return text.strip().strip('"').strip()


class LlmSummarizerAnalyzer:
    name = "llm_summarizer"
    stage = 5

    def __init__(self, llm: OllamaLLM | None = None) -> None:
        self._llm = llm or OllamaLLM(model=settings.summary_model)

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        text = (item.text or "").strip()
        if not (item.title or text):
            return
        # слишком короткий текст (типичный TG-пост) саммарить смысла мало — берём как есть
        if len(text) < 200 and item.title:
            ctx.summary = text or None
            return
        user = f"Заголовок: {item.title or '—'}\n\nТекст:\n{text[:4000]}"
        try:
            raw = await self._llm.chat_text(_SYSTEM, user, think=False)
        except Exception as e:  # best-effort
            log.warning("llm_summarizer_failed", item=ctx.item_id, error=str(e))
            return
        summary = _clean(raw)
        ctx.summary = summary or None
