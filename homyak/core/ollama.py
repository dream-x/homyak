"""Выбор Ollama-хоста: основной (GPU-бокс) с фолбэком на локальный.

Зачем отдельный модуль: URL раньше был прибит в llm.py и embedder.py по отдельности, а хосты
у нас теперь два — 5090-бокс (быстрый, но в другой подсети и может быть выключен) и локальная
Ollama на Metal. Фолбэк нужен ПО ПАРЕ (хост, модель): на боксе лежит qwen3.6:27b, а на Mac её
нет — там qwen3.5:9b. Просто переключить URL недостаточно, прилетит "model not found".
"""

from __future__ import annotations

from homyak.core.config import settings


def targets(model: str | None = None) -> list[tuple[str, str | None]]:
    """[(url, model)] по приоритету: основной хост, затем запасной со своей моделью.

    model=None — для эмбеддера: bge-m3 одинаковая на обоих хостах, подмены не нужно.
    """
    primary = (settings.ollama_url, model)
    if not settings.ollama_url_fallback:
        return [primary]

    fb_model = model
    # Модель подменяем только если она основная: явно заданную (напр. summary-фолбэк) не трогаем.
    if model and settings.llm_model_fallback and model == settings.llm_model:
        fb_model = settings.llm_model_fallback
    return [primary, (settings.ollama_url_fallback, fb_model)]
