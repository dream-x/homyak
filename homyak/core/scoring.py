"""Чистые функции скоринга. Легко тестируются, не зависят от БД."""

from __future__ import annotations

import math
from datetime import datetime, timezone

FRESHNESS_TAU_HOURS = 48.0


def freshness(published_at: datetime | None, now: datetime | None = None) -> float:
    """Экспоненциальный спад свежести: 1.0 сейчас → ~0.37 через TAU часов."""
    now = now or datetime.now(timezone.utc)
    if published_at is None:
        age_h = 24.0
    else:
        age_h = max(0.0, (now - published_at).total_seconds() / 3600.0)
    return math.exp(-age_h / FRESHNESS_TAU_HOURS)


def base_score(
    published_at: datetime | None,
    raw_score: float | None,
    cluster_size: int,
    now: datetime | None = None,
) -> float:
    """Базовый (не персональный) скор: свежесть · внешняя оценка · размер кластера.

    Персональный скор (Phase 6) заменит эту формулу гибридным ранкером под интересы.
    """
    raw = raw_score or 0.0
    size = max(1, cluster_size)
    return freshness(published_at, now) * (1.0 + raw) * (1.0 + math.log1p(size - 1))
