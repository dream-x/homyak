"""Analyzer stage 4: теги через qwen2.5:14b (JSON). Best-effort — не блокирует item при сбое."""

from __future__ import annotations

import structlog

from homyak.core.interfaces import AnalyzerContext
from homyak.core.llm import OllamaLLM

log = structlog.get_logger(__name__)

# словарь-затравка; свободные теги тоже разрешены
VOCAB = [
    "ai",
    "ai-agents",
    "llm",
    "ml-research",
    "rust",
    "python",
    "systems",
    "security",
    "web",
    "devtools",
    "startups",
    "hardware",
    "science",
    "crypto",
    "politics",
    "business",
]

_SYSTEM = (
    "Ты классифицируешь новости по темам. Верни строго JSON вида {\"tags\": [\"...\"]} — "
    "до 5 тегов, короткие, в нижнем регистре. Предпочитай теги из словаря, но можешь добавить свои."
)


class LlmTaggerAnalyzer:
    name = "llm_tagger"
    stage = 4

    def __init__(self, llm: OllamaLLM | None = None) -> None:
        self._llm = llm or OllamaLLM()

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        title = item.title or ""
        body = (item.text or "")[:1500]
        if not (title or body):
            return
        user = f"Словарь тем: {', '.join(VOCAB)}\n\nЗаголовок: {title}\nТекст: {body}"
        try:
            data = await self._llm.chat_json(_SYSTEM, user)
        except Exception as e:  # best-effort: теги опциональны, не роняем item
            log.warning("llm_tagger_failed", item=ctx.item_id, error=str(e))
            return
        tags = data.get("tags") if isinstance(data, dict) else None
        if isinstance(tags, list):
            ctx.tags = [t.lower().strip() for t in tags if isinstance(t, str) and t.strip()][:5]
