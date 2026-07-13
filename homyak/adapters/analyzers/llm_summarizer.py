"""Analyzer stage 5: качественное саммари через большую модель (SUMMARY_MODEL). Best-effort."""

from __future__ import annotations

import re

import structlog

from homyak.core.config import settings
from homyak.core.interfaces import AnalyzerContext
from homyak.core.llm import OllamaLLM

log = structlog.get_logger(__name__)

_SYSTEM = (
    "You are a sharp senior software engineer writing quick summaries for a fellow developer's feed. "
    "Summarize the article ENTIRELY in its original language (Russian article -> Russian, English -> English). "
    "Write with an engaging, techie voice — surface the technical substance, the engineering angle and why "
    "it actually matters to a developer; make it genuinely interesting to read, never dry PR-speak. "
    "First, 1-2 punchy sentences on the gist and why it's worth the time. Then, on new lines, "
    "2-3 short takeaways, each starting with '• ', stating concretely what the reader will learn or the key "
    "technical insight. Strictly from the text, no speculation. No preamble, no quotes, no meta, no hashtags."
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _clean(text: str) -> str:
    text = _THINK_RE.sub("", text)  # у thinking-моделей вырезаем reasoning, если просочился
    return text.strip().strip('"').strip()


class LlmSummarizerAnalyzer:
    name = "llm_summarizer"
    stage = 5

    def __init__(self, llm: OllamaLLM | None = None) -> None:
        self._llm = llm or OllamaLLM(
            model=settings.summary_model, fallback=settings.summary_fallback_model
        )

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        text = (item.text or "").strip()
        if not (item.title or text):
            return
        # Твиты и короткие посты не суммаризируем: гонять большую модель ради пары строк
        # расточительно (и забивает processor) — сам текст и есть контент.
        feed = item.feed_name or ""
        if feed.startswith("tw_") or (len(text) < 280 and item.title):
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
