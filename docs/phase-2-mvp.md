# Phase 2 — MVP feedthrough

## Цель

Первая версия с реальным потоком данных: **Miniflux + RSS → Postgres → processor (URL-дедуп) → FastAPI + RSS/JSON feed out**. Включается NATS JetStream как внутренняя шина. Без Telegram, без Ollama, без Qdrant — это всё Phase 3.

Успех = подписанные в `config/sources.yaml` RSS-фиды реально попадают в `/feed.rss` и `/feed.json`, дубликаты по URL склеиваются в один `cluster_id`, processor маркирует `processed_at` через JetStream event, всё работает в 3 отдельных процессах.

## Prerequisites

- Phase 1 завершена: Postgres поднят, миграция 0001 применена.

## Что в scope

- `docker-compose.yml` дополняется `nats` (qdrant/ollama уже есть, но в Phase 2 не используются)
- `homyak/core/interfaces.py` — Protocol'ы `PollSource`, `PushSource`, `Analyzer`, `OutputAdapter`, dataclass'ы `FeedQuery`, `Feed`, `AnalysisResult`
- `homyak/core/events.py` — NATS JetStream publish/consume helpers
- `homyak/core/registry.py` — регистратор источников/анализаторов/выходов через entry points или явный list
- `homyak/core/config.py` расширяется — загрузка `config/sources.yaml`
- `homyak/core/models.py` добавляется pydantic DTO `NewsItem` (конвертация в ORM/из ORM)
- `homyak/storage/postgres.py` — репозиторий
- `homyak/adapters/sources/miniflux.py` + `rss.py`
- `homyak/adapters/analyzers/url_dedup.py`
- `homyak/pipeline/ingest_poll.py` — APScheduler runner для PollSource'ов
- `homyak/pipeline/processor.py` — JetStream consumer на `items.ingested`, прогоняет analyzer pipeline
- `homyak/pipeline/sweeper.py` — fallback re-publish для зависших items (крон раз в 5 мин)
- `homyak/adapters/outputs/api.py` + `rss_out.py` + `json_feed.py` — FastAPI routes
- `homyak/pipeline/serve.py` — uvicorn entrypoint
- Entry points в `pyproject.toml`: `homyak-ingest-poll`, `homyak-processor`, `homyak-api`, `homyak-sweeper`
- Конфиг `config/sources.yaml` с реальным набором RSS
- Минимальные тесты (pytest + pytest-asyncio)

## Что НЕ в scope

- Telegram / tscrapper
- Qdrant + embeddings + similarity dedup
- Ollama / LLM analyzers
- SSE output
- Telegram-бот, CLI
- Twitter
- Web UI

---

## Компоненты

### 1. NATS JetStream

**docker-compose.yml** (добавка к Phase 1):
- Сервис `nats` уже в compose с Phase 1 — включаем использование.

**Stream setup** — скрипт `scripts/setup-nats.sh` или idempotent init в `core/events.py`:
- Stream `HOMYAK`, subjects `homyak.items.*`, storage=file, max_age=14d, max_bytes=5GB, replicas=1
- Создаётся при первом старте любого процесса через `jetstream.add_stream(..., check=True)`

**`homyak/core/events.py`**:
- `NatsBus` — async класс, держит connection + JetStreamContext
- `async def publish_ingested(news_item_id: int) -> None` — publish в `homyak.items.ingested`
- `async def publish_processed(news_item_id: int, cluster_id: int) -> None` — в `homyak.items.processed`
- `async def consume_ingested(handler, durable="processor") -> None` — pull consumer, `ack_wait=120s`, `max_deliver=5`
- Retry при ошибке connect (backoff до 30s), structured logs

### 2. Core interfaces

**`homyak/core/interfaces.py`** — Protocol'ы + dataclass'ы:

```python
@dataclass(slots=True)
class NewsItemDTO:
    source_type: str
    source_id: str
    url: str | None
    title: str | None
    text: str | None
    media: list[str]
    author: str | None
    raw_score: float | None
    published_at: datetime | None
    category: str | None
    # после обработки:
    tags: list[str] | None = None
    cluster_id: int | None = None

class PollSource(Protocol):
    name: str
    interval_seconds: int
    async def poll(self, cursor: str | None) -> AsyncIterator[tuple[NewsItemDTO, str]]: ...

class PushSource(Protocol):
    name: str
    async def subscribe(self, sink: Callable[[NewsItemDTO], Awaitable[None]]) -> None: ...

@dataclass(slots=True)
class AnalysisResult:
    cluster_id: int | None = None
    tags: list[str] | None = None
    summary: str | None = None
    score: float | None = None
    embedding: list[float] | None = None

class Analyzer(Protocol):
    name: str
    stage: int
    async def analyze(self, item_id: int, session: AsyncSession) -> AnalysisResult: ...

@dataclass(slots=True)
class FeedQuery:
    category: str | None = None
    source_types: list[str] | None = None
    min_score: float | None = None
    since: datetime | None = None
    collapse_clusters: bool = True
    limit: int = 100
    cursor: str | None = None

@dataclass(slots=True)
class Feed:
    items: list[NewsItemDTO]
    next_cursor: str | None

class OutputAdapter(Protocol):
    name: str
    async def serve(self, query: FeedQuery, session: AsyncSession) -> Feed: ...
```

### 3. Config loader

**`homyak/core/config.py`** расширяется:
- `Settings` (pydantic-settings): `database_url`, `nats_url`, `sources_config_path = "config/sources.yaml"`
- Функция `load_sources_config() -> SourcesConfig` — читает YAML, валидирует через pydantic model
- `SourcesConfig`:
  - `miniflux: MinifluxConfig | None` (`enabled`, `base_url`, `token`, `interval_seconds`, `categories`)
  - `rss: list[RSSFeedConfig]` (`name`, `url`, `category`, `interval_seconds`, `weight`)
- Токены — **не из YAML**, из env. В YAML указываем env-var name: `token_env: MINIFLUX_TOKEN`

### 4. Storage

**`homyak/storage/postgres.py`** — репозиторий на SQLAlchemy:

```python
class NewsRepo:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]): ...

    async def upsert_item(self, item: NewsItemDTO) -> tuple[int, bool]:
        """Returns (id, was_new). INSERT ... ON CONFLICT (source_type, source_id) DO UPDATE."""

    async def get_by_id(self, id_: int) -> NewsItem | None: ...

    async def get_cursor(self, source_name: str) -> str | None: ...
    async def save_cursor(self, source_name: str, cursor: str, error: str | None = None) -> None: ...

    async def mark_processed(self, id_: int, result: AnalysisResult) -> None: ...
    async def mark_failed(self, id_: int, error: str) -> None:
        """attempts += 1, retry_after = now() + 2^attempts min"""

    async def feed(self, query: FeedQuery) -> Feed:
        """Collapses clusters, filters by category/source_types/since, cursor-paginated."""

    async def unprocessed_stale(self, older_than_minutes: int = 5) -> list[int]:
        """For sweeper: processed_at IS NULL AND fetched_at < now() - interval AND attempts < 5"""
```

### 5. Source adapters

**`homyak/adapters/sources/miniflux.py`** (PollSource):
- `name = "miniflux"`, `interval_seconds` из конфига
- `poll(cursor)` — вызывает Miniflux REST API `GET /v1/entries?after_entry_id={cursor}&limit=100&order=id&direction=asc`
- Для каждого entry → `NewsItemDTO(source_type="miniflux", source_id=str(entry.id), ...)`
- cursor = максимальный `entry.id` после итерации

**`homyak/adapters/sources/rss.py`** (PollSource, универсальный):
- Инстанцируется для каждого фида из `config/sources.yaml`
- `name = f"rss:{config.name}"`
- `poll(cursor)` — `httpx.get(url)` + `feedparser.parse()` (в `asyncio.to_thread`)
- `source_id = entry.id` или `hash(entry.link + title)` как fallback
- cursor = ISO timestamp последнего обработанного `published_parsed`; items с `published <= cursor` пропускаем
- `media` — из `entry.media_content` и `entry.enclosures`

### 6. Analyzer: URL dedup

**`homyak/adapters/analyzers/url_dedup.py`**:
- `stage = 1`
- `analyze(item_id, session)`:
  1. Берём item, нормализуем URL (урезаем `utm_*`, `fbclid`, `gclid`; сортируем query params; убираем fragment)
  2. SELECT `cluster_id` из news_items WHERE нормализованный URL совпадает, кроме текущего id
  3. Если нашли → возвращаем `AnalysisResult(cluster_id=<found>)`
  4. Если нет — создаём новый `Cluster`, возвращаем его id
- URL-нормализация: отдельная чистая функция `normalize_url(url) -> str` — легко тестируется
- Хранилище нормализованного URL: отдельная колонка `url_normalized` добавляется миграцией 0002 с индексом, либо функциональный индекс через expression. Упрощение: миграция 0002 в Phase 2 добавляет `url_normalized` как GENERATED column (postgres immutable function? нет — нужен trigger или python-side compute). **Решение**: python-side — `NewsRepo.upsert_item` заполняет `url_normalized` перед insert'ом. Индекс `idx_news_url_normalized`.

### 7. Ingest poll runner

**`homyak/pipeline/ingest_poll.py`**:
- Загружает `sources.yaml`, инстанцирует включённые PollSource'ы
- APScheduler `AsyncIOScheduler` с `IntervalTrigger(seconds=source.interval_seconds)`
- На каждый tick:
  1. `cursor = repo.get_cursor(source.name)`
  2. async for `(item, new_cursor) in source.poll(cursor)`:
     - `id_, was_new = await repo.upsert_item(item)`
     - если `was_new`: `await bus.publish_ingested(id_)`
  3. `repo.save_cursor(source.name, final_cursor)`
- Graceful shutdown на SIGTERM/SIGINT: дожидаем inflight poll'ы

### 8. Processor

**`homyak/pipeline/processor.py`**:
- Один процесс = один durable consumer `processor` на subject `homyak.items.ingested`
- Analyzers упорядочены по `stage`. В Phase 2 — только `url_dedup`.
- Loop:
  1. `msg = await sub.next_msg(timeout=...)`
  2. parse `{news_item_id}`
  3. async with session: прогон analyzer pipeline
     - если stage вернул `cluster_id` — сохраняем в item'е
     - merge результатов в `AnalysisResult`
  4. `repo.mark_processed(id, combined_result)` + `bus.publish_processed(...)`
  5. `msg.ack()`
  6. При exception — `msg.nak(delay=2**attempts * 60)`, `repo.mark_failed(id, str(exc))`
- Circuit breaker — Phase 3 (сейчас не нужен, Ollama ещё нет)
- Параллелизм: `concurrent.PullSubscription` или несколько инстансов на один `durable_name` — JetStream делит работу

### 9. Sweeper

**`homyak/pipeline/sweeper.py`**:
- APScheduler `CronTrigger(minute='*/5')`
- `stale = await repo.unprocessed_stale(older_than_minutes=5)` (partial index уже есть из Phase 1)
- Для каждого: `bus.publish_ingested(id_)` снова
- Используем `WHERE attempts < 5` чтобы не зациклить dead-letter

### 10. Outputs

**`homyak/adapters/outputs/api.py`** — FastAPI routes:
- `GET /healthz` — проверка PG + NATS, `{status: "ok"}`
- `GET /feed` — JSON со списком items, query params из FeedQuery
- `GET /item/{id}` — детали item'а
- `GET /feed.rss` — делегирует `rss_out.py`
- `GET /feed.json` — делегирует `json_feed.py`
- `POST /admin/sources/{name}/repoll` — форс-перечитать source (dev-helper)
- `POST /admin/clusters/{id}/split` — ручной split cluster'а (защита от false-positive, пригодится в Phase 3)

**`homyak/adapters/outputs/rss_out.py`**:
- feedgen: title "Homyak Feed", поддерживает `?category=...` и т.д.
- items: cluster representatives (не все дубликаты)

**`homyak/adapters/outputs/json_feed.py`**:
- JSON Feed 1.1 (https://www.jsonfeed.org/version/1.1/)
- items аналогично RSS

**`homyak/pipeline/serve.py`**:
- `uvicorn.run("homyak.adapters.outputs.api:app", host="0.0.0.0", port=8000)`

### 11. Entry points (pyproject.toml)

```toml
[project.scripts]
homyak-ingest-poll = "homyak.pipeline.ingest_poll:main"
homyak-processor   = "homyak.pipeline.processor:main"
homyak-sweeper     = "homyak.pipeline.sweeper:main"
homyak-api         = "homyak.pipeline.serve:main"
```

### 12. Новые зависимости (pyproject.toml)

Добавляется к Phase 1:
- `fastapi>=0.110`
- `uvicorn[standard]>=0.27`
- `httpx>=0.27`
- `feedparser>=6.0`
- `miniflux>=1.0` (Python SDK)
- `nats-py>=2.9`
- `apscheduler>=3.10`
- `pyyaml>=6.0`
- `feedgen>=1.0`
- Dev: `pytest>=8.0`, `pytest-asyncio>=0.23`, `pytest-httpx`, `faker`

### 13. Миграция 0002

Добавляет:
- `news_items.url_normalized` TEXT
- `CREATE INDEX idx_news_url_normalized ON news_items(url_normalized) WHERE url_normalized IS NOT NULL`
- FK `clusters.representative_id → news_items.id` (не добавленный в 0001 из-за циклики)
- Backfill `url_normalized` для существующих строк (на Phase 1 их 0, операция noop, но код безопасный)

### 14. Тесты (минимум)

- `tests/test_url_normalize.py` — нормализация URL, edge cases
- `tests/test_repo_upsert.py` — `upsert_item` идемпотентен, `was_new` правильный
- `tests/test_repo_cursor.py` — сохранение/чтение cursor
- `tests/test_miniflux_adapter.py` — мок Miniflux API через `respx`, корректный парсинг entries
- `tests/test_rss_adapter.py` — `respx` + фиксированный RSS XML, корректный cursor advance
- `tests/test_url_dedup_analyzer.py` — 2 item'а с одинаковым URL → один cluster
- `tests/test_feed_endpoint.py` — httpx test client, collapse_clusters, пагинация

Используем `pytest-asyncio` с `asyncio_mode = auto`. PG — через testcontainers или локальный compose-up в fixture.

---

## Конфиг `config/sources.yaml` (стартовый набор)

```yaml
miniflux:
  enabled: true
  base_url: http://localhost:8080
  token_env: MINIFLUX_TOKEN
  interval_seconds: 300
  categories: [tech, ai]  # опционально, фильтр

rss:
  - {name: hn,             url: "https://news.ycombinator.com/rss",        category: tech,         interval_seconds: 600, weight: 1.0}
  - {name: habr_best,      url: "https://habr.com/ru/rss/best/daily/",     category: tech,         interval_seconds: 3600, weight: 0.9}
  - {name: lobsters,       url: "https://lobste.rs/rss",                   category: tech,         interval_seconds: 900, weight: 0.8}
  - {name: arxiv_cs_cl,    url: "http://export.arxiv.org/rss/cs.CL",       category: ai_research,  interval_seconds: 3600, weight: 1.0}
  - {name: arxiv_cs_lg,    url: "http://export.arxiv.org/rss/cs.LG",       category: ai_research,  interval_seconds: 3600, weight: 1.0}
  - {name: huggingface,    url: "https://huggingface.co/blog/feed.xml",    category: ai,           interval_seconds: 3600, weight: 0.9}
  - {name: paperswithcode, url: "https://paperswithcode.com/feeds/latest", category: ai_research,  interval_seconds: 3600, weight: 0.9}
  - {name: techcrunch,     url: "https://techcrunch.com/feed/",            category: tech,         interval_seconds: 900, weight: 0.7}
  - {name: the_verge,      url: "https://www.theverge.com/rss/index.xml",  category: tech,         interval_seconds: 900, weight: 0.7}
  - {name: ars,            url: "https://feeds.arstechnica.com/arstechnica/index", category: tech, interval_seconds: 1800, weight: 0.8}
```

---

## Acceptance criteria

```bash
cd /Users/maks/projects/homyak

# 1. Инфра
docker compose up -d postgres nats

# 2. Миграция
uv run alembic upgrade head  # применит 0001 и 0002

# 3. Конфиг Miniflux в .env
echo "MINIFLUX_TOKEN=..." >> .env

# 4. Запуск трёх процессов в терминалах
uv run homyak-ingest-poll   # терминал 1
uv run homyak-processor     # терминал 2
uv run homyak-api           # терминал 3
uv run homyak-sweeper &     # фоном

# 5. Подождать 1-2 цикла (10-15 мин из-за интервалов RSS)
#    либо POST /admin/sources/rss:hn/repoll для ускорения

# 6. Проверки
psql "postgresql://homyak:homyak@localhost:5432/homyak" -c "SELECT source_type, count(*) FROM news_items GROUP BY 1"
# Видим miniflux, rss строки

psql ... -c "SELECT cluster_id, count(*) FROM news_items GROUP BY cluster_id HAVING count(*) > 1"
# Кластеры с size > 1 — URL-дедуп сработал (HN часто дублирует TechCrunch и т.д.)

curl -s localhost:8000/feed?limit=20 | jq '.items | length'
# >0, <= 20

curl -s localhost:8000/feed.rss | xmllint --noout -
# Валидный XML

curl -s localhost:8000/feed.json | jq '.version'
# "https://jsonfeed.org/version/1.1"

nats stream info HOMYAK
nats consumer info HOMYAK processor
# Есть stream, consumer, delivered/acked счётчики растут
```

---

## Checklist

- [ ] `docker-compose.yml` использует nats (не только образ — stream создаётся)
- [ ] Миграция 0002 применяется без ошибок, `url_normalized` + FK `representative_id` есть
- [ ] `core/events.py` создаёт stream + consumer идемпотентно
- [ ] `core/interfaces.py` компилируется, все Protocol'ы определены
- [ ] `storage/postgres.py` `upsert_item` проходит unit test на идемпотентность
- [ ] `adapters/sources/miniflux.py` парсит entries и возвращает `(item, cursor)`
- [ ] `adapters/sources/rss.py` работает на HN и arXiv (cursor на ISO timestamp'е)
- [ ] `adapters/analyzers/url_dedup.py` создаёт новый cluster когда не найден, мержит в существующий когда найден
- [ ] `pipeline/ingest_poll.py` запускается, APScheduler тикает, publish'ит в NATS
- [ ] `pipeline/processor.py` — durable consumer, ack при успехе, nak при ошибке, dead-letter после 5 попыток
- [ ] `pipeline/sweeper.py` каждые 5 мин перепубликует зависшие items
- [ ] FastAPI `/healthz`, `/feed`, `/item/{id}`, `/feed.rss`, `/feed.json`, `/admin/sources/{name}/repoll`
- [ ] Все тесты из секции 14 зелёные
- [ ] `uv run alembic downgrade base && uv run alembic upgrade head` — повторяемо
