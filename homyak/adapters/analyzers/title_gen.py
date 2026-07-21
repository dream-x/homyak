"""Analyzer stage 3: заголовок для постов без него (Telegram/твиты/RSS-огрызки).

Многие источники не дают title — в ленте/канале/пуше они висели как «—». Генерируем
заголовок МОДЕЛЬЮ из текста поста (коротко, на языке текста). Если модели нет/упала или
текст совсем короткий — падаем на эвристику core.titles.derive_title (первая строка).
Мутирует item.title напрямую — processor персистит это на общем commit'е (как cluster_id).
Работает ПОСЛЕ prefilter: отсеянным заголовок ни к чему, и до summarizer (тот берёт title).
"""

from __future__ import annotations

import re

import structlog

from homyak.core.interfaces import AnalyzerContext
from homyak.core.llm import OllamaLLM
from homyak.core.titles import derive_title

log = structlog.get_logger(__name__)

_SYSTEM = (
    "Ты редактор новостной ленты для разработчика. По тексту поста придумай ОДИН короткий, "
    "точный заголовок — строго по сути текста, без домыслов. Пиши на языке поста "
    "(русский пост → русский заголовок, английский → английский). 4–10 слов. "
    "Без кавычек, эмодзи, хештегов, без точки в конце и без слов вроде 'Заголовок:'. "
    "Верни ТОЛЬКО сам заголовок одной строкой."
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MIN_FOR_LLM = 80  # короче — сам текст и есть заголовок, гонять модель незачем


async def make_title(llm: OllamaLLM | None, text: str | None, feed_name: str | None) -> str | None:
    """Заголовок из текста: сперва модель, при неудаче/коротком тексте — эвристика.

    Общая точка для стадии и бэкфилла. Медиа-пост без текста → мягкий фолбэк по фиду.
    """
    body = (text or "").strip()
    fallback = f"Запись {feed_name}" if feed_name else None
    if not body:
        return fallback

    if llm is not None and len(body) >= _MIN_FOR_LLM:
        try:
            raw = await llm.chat_text(_SYSTEM, body[:3000], think=False)
            # derive_title заодно чистит вывод модели: первая строка, без markdown/эмодзи, обрез
            title = derive_title(_THINK_RE.sub("", raw))
            if title:
                # модель любит обернуть в кавычки и поставить точку — заголовку это ни к чему
                title = title.strip(" .«»\"'“”").strip()
                if title:
                    return title
        except Exception as e:  # best-effort — ниже эвристика
            log.warning("title_gen_llm_failed", error=str(e)[:120])

    return derive_title(body, fallback=fallback)


class TitleGenAnalyzer:
    name = "title_gen"
    stage = 3  # после prefilter(3) по порядку в registry; строго до summarizer(5)

    def __init__(self, llm: OllamaLLM | None = None) -> None:
        self._llm = llm

    async def analyze(self, ctx: AnalyzerContext) -> None:
        item = ctx.item
        if item.title and item.title.strip():
            return
        title = await make_title(self._llm, item.text, item.feed_name)
        if title:
            item.title = title
