---
name: homyak-adapter
description: Добавить новый плагин-адаптер в Homyak — source (PollSource/PushSource), analyzer (стадия pipeline) или output. Используй всегда, когда заводишь новый источник новостей, стадию обработки/анализатор или выход ленты. Кодирует контракты из core/interfaces.py, регистрацию в registry и паттерн тестирования.
---

# Homyak: добавление адаптера

Ядро Homyak ничего не знает о конкретных источниках/анализаторах/выходах — всё через три
Protocol-контракта в `homyak/core/interfaces.py`. Никогда не хардкодь источник в pipeline —
добавляй адаптер и регистрируй его.

## Три типа

| Тип | Protocol | Где файл | Что делает |
|---|---|---|---|
| Source (poll) | `PollSource` | `adapters/sources/<name>.py` | Периодически тянет items (RSS, Miniflux). Ядро крутит через APScheduler. |
| Source (push) | `PushSource` | `adapters/sources/<name>.py` | Long-running подписка (telegram-relay). Ядро запускает как task. |
| Analyzer | `Analyzer` | `adapters/analyzers/<name>.py` | Стадия обработки. Мутирует `AnalyzerContext`. Упорядочены по `stage`. |
| Output | `OutputAdapter` | `adapters/outputs/<name>.py` | Отдаёт `Feed` по `FeedQuery` (REST/RSS/SSE/бот). |

## Контракты (см. core/interfaces.py — источник истины)

```python
class PollSource(Protocol):
    name: str
    interval_seconds: int
    async def poll(self, cursor: str | None) -> AsyncIterator[tuple[NewsItemDTO, str]]: ...

class PushSource(Protocol):
    name: str
    async def subscribe(self, sink: Callable[[NewsItemDTO], Awaitable[None]]) -> None: ...

class Analyzer(Protocol):
    name: str
    stage: int
    async def analyze(self, ctx: AnalyzerContext) -> None: ...   # мутирует ctx, не возвращает

class OutputAdapter(Protocol):
    name: str
    async def serve(self, query: FeedQuery, session: AsyncSession) -> Feed: ...
```

## Чеклист нового source-адаптера

1. Файл `adapters/sources/<name>.py`, класс реализует `PollSource` или `PushSource`.
2. `source_type` в `NewsItemDTO` — стабильная строка (`"rss"`, `"miniflux"`, `"telegram"`).
3. `source_id` — стабильный уникальный ключ внутри источника (для UNIQUE `(source_type, source_id)`).
   Fallback при отсутствии id: детерминированный хеш (`link+title`), НЕ случайный.
4. `cursor` — сериализуемая строка (max entry id / ISO timestamp). Возвращается вместе с item'ом.
5. Никаких сайд-эффектов записи в БД внутри адаптера — только выдаёт DTO. Upsert/publish делает runner.
6. Сетевые вызовы — через `httpx` (async) либо `asyncio.to_thread` для sync-библиотек (feedparser).
7. Зарегистрируй в `core/registry.py` (или в `config/sources.yaml` для конфигурируемых RSS).

## Чеклист нового analyzer-адаптера

1. Файл `adapters/analyzers/<name>.py`, `stage: int` определяет порядок (см. architecture.md § stages).
2. `analyze(ctx)` **мутирует** `AnalyzerContext` (пишет `ctx.embedding` / `ctx.cluster_id` / `ctx.tags` /
   `ctx.llm_relevance` / `ctx.personal_score`), ничего не возвращает.
3. Читай уже проставленные поля ctx от предыдущих стадий, не дёргай БД/Qdrant повторно.
4. Идемпотентность: если результат уже посчитан (кэш-колонка в `news_items` не NULL и версия совпадает) — skip.
5. Внешние сервисы (Ollama) — оборачивай в circuit breaker (`core/circuit.py`), при недоступности `raise`
   → processor делает `nak` + retry.
6. Зарегистрируй в processor'е (список analyzer'ов сортируется по `stage`).

## Чеклист нового output-адаптера

1. Файл `adapters/outputs/<name>.py`.
2. `serve(query, session)` строит `Feed` через `NewsRepo.feed(query)` — не пиши SQL заново, переиспользуй repo.
3. Отдавай **representatives кластеров** (не все дубликаты), если `query.collapse_clusters`.
4. REST-выходы монтируются в `adapters/outputs/api.py` через `include_router`.

## Тест адаптера

- Source: мок сети (`respx`/`pytest-httpx`), фиксированный ответ → проверь корректный DTO и advance курсора.
- Analyzer: собери `AnalyzerContext` вручную, вызови `analyze(ctx)`, проверь мутацию ctx.
- Держи чистую логику (нормализация URL, свёртка score) в отдельных функциях — тестируй их без БД.

## Не делай

- Не хардкодь source/analyzer/output в `pipeline/*` — только через registry/config.
- Не пиши в БД из source-адаптера. Не возвращай результат из analyzer (мутируй ctx).
- Не тяни один и тот же вектор/строку дважды между стадиями — используй ctx.
