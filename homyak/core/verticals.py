"""Тематические вертикали: business / it / medical. Каждая — независимая персонализация."""

from __future__ import annotations

VERTICALS: tuple[str, ...] = ("business", "it", "medical")
DEFAULT_VERTICAL = "it"

LABELS = {
    "business": "💼 Business",
    "it": "💻 IT",
    "medical": "🩺 Medical",
}


def norm_vertical(v: str | None) -> str | None:
    """Нормализует значение вертикали; всё вне списка (other/пусто) → None."""
    v = (v or "").lower().strip()
    return v if v in VERTICALS else None
