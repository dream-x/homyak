"""Чистые функции скоринга. Легко тестируются, не зависят от БД."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
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


# --- Phase 6: персональный (гибридный) скор ---


@dataclass(frozen=True)
class PersonalWeights:
    llm: float = 0.50
    taste: float = 0.20
    tag: float = 0.15
    source: float = 0.10
    fresh: float = 0.05


def weights_from_interests() -> PersonalWeights:
    """Веса свёртки из config/interests.yaml (секция weights) — там же, где сами интересы."""
    from homyak.core.interests import weights

    w = weights()
    return PersonalWeights(llm=w.llm, taste=w.taste, tag=w.tag, source=w.source, fresh=w.fresh)


def cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def centroid_add(
    centroid: Sequence[float] | None, n: int, v: Sequence[float]
) -> tuple[list[float], int]:
    """Инкрементально добавить вектор в центроид (running mean). Обратимо через centroid_remove."""
    if not centroid or n <= 0:
        return list(v), 1
    return [(c * n + x) / (n + 1) for c, x in zip(centroid, v)], n + 1


def centroid_remove(
    centroid: Sequence[float] | None, n: int, v: Sequence[float]
) -> tuple[list[float] | None, int]:
    """Убрать вектор из центроида (точная инверсия centroid_add при известном n)."""
    if not centroid or n <= 1:
        return None, 0
    return [(c * n - x) / (n - 1) for c, x in zip(centroid, v)], n - 1


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def personal_score(
    *,
    llm_relevance: float | None,
    taste_cos: float | None,
    tag_aff: float | None,
    source_aff: float | None,
    freshness_val: float | None,
    n_liked: int,
    weights: PersonalWeights,
    taste_ramp: int = 20,
) -> float:
    """Гибридная свёртка. taste-компонента наращивается по мере накопления лайков (cold-start ramp)."""
    ramp = min(1.0, n_liked / taste_ramp) if taste_ramp > 0 else 1.0
    taste_w = weights.taste * ramp
    return (
        weights.llm * (llm_relevance or 0.0)
        + taste_w * (taste_cos or 0.0)
        + weights.tag * (tag_aff or 0.0)
        + weights.source * (source_aff or 0.0)
        + weights.fresh * (freshness_val or 0.0)
    )
