"""Интересы: единственное место, где задаётся «что мне нравится» (config/interests.yaml).

Три слоя, между ними стена:

  1. ДЕКЛАРАЦИЯ — этот файл. Твои слова: description + topics вертикалей, watch, weights.
  2. ВЫУЧЕННОЕ  — tag_affinity / source_affinity / taste / muted_tags в БД. Копится из 👍/👎.
  3. ВЕСА       — секция weights: сколько слой 2 вообще значит.

Стена односторонняя и это принципиально: слой 2 НЕ пишет в слой 1. Раньше писал — кнопка 🔇
дёргала set_profile и переписывала твой текст, мьютя первый тег статьи. У медицинской статьи
первый тег — `medical`, поэтому одно нажатие выключило 21% вертикали, и заметить это было
неоткуда: hard-mute даже не писал skip_reason.

Кэш по mtime, а не lru_cache: файл смонтирован в контейнеры volume'ом (ro), правки watch/weights
должны подхватываться без рестарта. verticals кэшу не подчиняются — они версионируются в БД
через `homyak-interests apply`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, Field, field_validator

from homyak.core.config import settings
from homyak.core.tags import normalize_tag
from homyak.core.verticals import VERTICALS
from homyak.core.watchlist import CompiledTopic, compile_watchlist

log = structlog.get_logger(__name__)

Polarity = Literal["love", "like", "meh", "dislike", "mute"]

# Префикс skip_reason для hard-mute. Константа, а не литерал в двух местах: по нему
# `homyak-interests backfill` находит жертв снятого мьюта. С литералом в проза-строке
# рассинхрон был бы вопросом времени, а ценой — навсегда мёртвые айтемы.
MUTE_SKIP_PREFIX = "мьют темы: "


class Topic(BaseModel):
    name: str
    polarity: Polarity = "like"


class VerticalInterest(BaseModel):
    description: str = ""
    topics: list[Topic] = Field(default_factory=list)


class WatchTopic(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    boost: bool = True
    # пусто = любая вертикаль; иначе после теггера метка снимается с записей других вертикалей
    # (вертикаль неизвестна → остаётся)
    verticals: list[str] = Field(default_factory=list)

    @field_validator("verticals")
    @classmethod
    def _known_verticals(cls, v: list[str]) -> list[str]:
        # опечатка (buisness) ловится при разборе: процессы держатся за последнюю рабочую
        # версию файла, CLI печатает «не разобран» (load_error) и выходит с ошибкой
        unknown = sorted(set(v) - set(VERTICALS))
        if unknown:
            raise ValueError(f"watch: неизвестные вертикали {unknown}; есть {list(VERTICALS)}")
        return v


class Weights(BaseModel):
    """Дефолты = поведение до вынесения весов в файл; отсутствующий ключ ничего не ломает."""

    llm: float = 0.50
    taste: float = 0.20
    tag: float = 0.15
    source: float = 0.10
    fresh: float = 0.05
    taste_ramp: int = 20
    watchlist_boost: float = 0.15
    push_threshold: float = 0.55
    prefilter_min_sim: float = 0.35


class Interests(BaseModel):
    verticals: dict[str, VerticalInterest] = Field(default_factory=dict)
    watch: list[WatchTopic] = Field(default_factory=list)
    weights: Weights = Field(default_factory=Weights)


# path -> (mtime, Interests, скомпилированный watch). mtime=-1 → файла нет.
_CACHE: dict[str, tuple[float, Interests, list[CompiledTopic]]] = {}
# Последняя УСПЕШНО разобранная версия — подстилка под битую правку (см. _load).
_LAST_GOOD: dict[str, tuple[Interests, list[CompiledTopic]]] = {}
# Почему текущая версия файла не разобрана (для CLI: «нет в файле» и «файл битый» — разные вещи).
_LOAD_ERROR: dict[str, str] = {}


def _resolve(path: str | None) -> str:
    return str(path or settings.interests_path)


def _load(path: str | None = None) -> tuple[Interests, list[CompiledTopic]]:
    p = _resolve(path)
    try:
        mtime = Path(p).stat().st_mtime
    except OSError:
        mtime = -1.0
    hit = _CACHE.get(p)
    if hit is not None and hit[0] == mtime:
        return hit[1], hit[2]

    if mtime < 0:
        # Молча уехать на дефолты нельзя: prefilter_min_sim решает, что вообще дойдёт до LLM,
        # push_threshold — что прилетит в телегу. Подмена таких ручек обязана быть громкой.
        log.error("interests_file_missing", path=p, effect="работаем на дефолтах")
        parsed, compiled = Interests(), []
    else:
        try:
            data = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
            parsed = Interests.model_validate(data)
            compiled = compile_watchlist([t.model_dump() for t in parsed.watch])
            _LOAD_ERROR.pop(p, None)
        except Exception as e:
            # Файл правится руками на живой системе, и нас зовёт prefilter на КАЖДЫЙ айтем
            # (stage 3). Пробрось мы исключение — опечатка роняла бы весь поток: mark_failed
            # → nak → после 5 попыток sweeper бросает айтемы навсегда. Поэтому держимся
            # за последнюю рабочую версию и кэшируем битый mtime, чтобы не парсить её заново
            # на каждый айтем.
            log.error("interests_invalid", path=p, error=str(e)[:200])
            _LOAD_ERROR[p] = str(e)[:300]
            good = _LAST_GOOD.get(p)
            parsed, compiled = good if good is not None else (Interests(), [])
            _CACHE[p] = (mtime, parsed, compiled)
            return parsed, compiled
    _CACHE[p] = (mtime, parsed, compiled)
    _LAST_GOOD[p] = (parsed, compiled)
    return parsed, compiled


def load_interests(path: str | None = None) -> Interests:
    return _load(path)[0]


def load_error(path: str | None = None) -> str | None:
    """Текст ошибки разбора текущей версии файла, если она не применилась (иначе None)."""
    _load(path)
    return _LOAD_ERROR.get(_resolve(path))


def compiled_watchlist(path: str | None = None) -> list[CompiledTopic]:
    """Предкомпилированные темы watch — для матчера (зовётся на каждый айтем)."""
    return _load(path)[1]


def watch_topic_names(path: str | None = None) -> list[str]:
    return [t.name for t in load_interests(path).watch]


def no_boost_topics(path: str | None = None) -> frozenset[str]:
    """Темы с `boost: false` — видим (👁, панель, мимо гейта), но в ранге не поднимаем."""
    return frozenset(t.name for t in load_interests(path).watch if not t.boost)


def weights(path: str | None = None) -> Weights:
    return load_interests(path).weights


def fold_topics(topics: list[dict] | list[Topic]) -> tuple[dict[str, str], list[str]]:
    """Декларация → {канон темы: полярность} + предупреждения о схлопнувшихся парах.

    Теггер пишет теги в каноне (`core/tags`), и tag_affinity живёт под каноном, поэтому два
    написания одной темы в файле (observability + monitoring, self-hosted + self-hosting) — ОДНА
    тема. Первая в файле задаёт полярность; если вторая с ней спорит — это видно в diff/apply,
    а не молча «последний побеждает» (так один apply ронял посев дублем ключа, а каждый
    следующий перетирал выученный вес группы — ревью круга 3).
    """
    folded: dict[str, str] = {}
    first: dict[str, str] = {}
    warnings: list[str] = []
    for t in topics:
        raw = t.get("name") if isinstance(t, dict) else t.name
        if not raw:
            continue
        pol = (t.get("polarity", "like") if isinstance(t, dict) else t.polarity) or "like"
        canon = normalize_tag(raw) or raw
        if canon not in folded:
            folded[canon] = pol
            first[canon] = raw
        elif folded[canon] != pol:
            warnings.append(
                f"⚠ {first[canon]} и {raw} — одна тема ({canon}) с разной полярностью"
                f" ({folded[canon]} / {pol}); действует {folded[canon]}, первая в файле"
            )
    return folded, warnings


def diff_declaration(
    decl: VerticalInterest, db_description: str, db_topics: list[dict]
) -> list[str]:
    """Чем файл расходится с ПРИМЕНЁННЫМ профилем. Пусто = синхронизировано.

    Чистая функция (ни БД, ни файла) — её зовут и CLI (`homyak-interests diff`), и панель
    дашборда. Дрейф обязан быть видимым: `medical: mute` разошёлся с YAML и прожил трое суток
    незамеченным именно потому, что сравнить было нечем.
    """
    norm = lambda s: " ".join((s or "").split())  # noqa: E731
    lines: list[str] = []
    if norm(db_description) != norm(decl.description):
        lines.append("описание изменилось (его читает судья дословно)")
    db = {
        t["name"]: t.get("polarity", "like")
        for t in db_topics
        if isinstance(t, dict) and t.get("name")
    }
    fl = {t.name: t.polarity for t in decl.topics}
    lines += [f"+ {n} ({fl[n]})" for n in sorted(set(fl) - set(db))]
    lines += [f"− {n} (в БД было {db[n]})" for n in sorted(set(db) - set(fl))]
    lines += [f"~ {n}: {db[n]} → {fl[n]}" for n in sorted(set(db) & set(fl)) if db[n] != fl[n]]
    return lines
