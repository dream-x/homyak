"""Analyzer stage 4: теги через qwen2.5:14b (JSON). Best-effort — не блокирует item при сбое."""

from __future__ import annotations

import structlog

from homyak.core.interfaces import AnalyzerContext
from homyak.core.llm import OllamaLLM
from homyak.core.verticals import norm_vertical

log = structlog.get_logger(__name__)

# словарь-затравка; свободные теги тоже разрешены
VOCAB = [
    # AI / ML
    "ai",
    "ai-agents",
    "llm",
    "ml-research",
    # языки и программирование
    "python",
    "golang",
    "java",
    "rust",
    "javascript",
    "cpp",
    # системщина / инфра / backend
    "systems",
    "backend",
    "distributed-systems",
    "databases",
    "kubernetes",
    "devops",
    "networking",
    "performance",
    "security",
    "devtools",
    "web",
    # прочее
    "hardware",
    "science",
    "startups",
    "business",
    "crypto",
    "politics",
]

_SYSTEM = (
    "Ты классифицируешь новости. Верни строго JSON вида "
    '{"tags": ["..."], "vertical": "..."}.\n'
    "tags — до 5 коротких тегов в нижнем регистре (предпочитай словарь, можно свои).\n"
    "vertical — ОДНА тематическая вертикаль:\n"
    "• \"business\" — рынки, финансы, экономика, макро, инвестиции, настроение рынка, бизнес компаний;\n"
    "• \"it\" — технологии, разработка, AI/ML, инфраструктура, кибербезопасность, наука о данных;\n"
    "• \"medical\" — медицина, здравоохранение, биотех, фарма, здоровье, клинические исследования;\n"
    "• \"other\" — если не подходит ни одна из трёх."
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
        if not isinstance(data, dict):
            return
        tags = data.get("tags")
        if isinstance(tags, list):
            ctx.tags = [t.lower().strip() for t in tags if isinstance(t, str) and t.strip()][:5]
        vertical = norm_vertical(data.get("vertical"))
        ctx.vertical = vertical
        ctx.item.vertical = vertical  # персистится processor'ом
