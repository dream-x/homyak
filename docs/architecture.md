# Homyak — архитектура решения

## Назначение

Персональный агрегатор новостей, объединяющий разнородные источники (Telegram через tscrapper, Miniflux RSS, Twitter с кастомными оценками, популярные новостные сайты по RSS) в единую дедуплицированную ленту с локальной LLM-обработкой (bge-m3 + Qwen 2.5-14B через Ollama). Несколько выходов: Web UI, Telegram-бот, CLI/TUI, RSS/JSON feed наружу.

Ключевое архитектурное требование — **плагинная система адаптеров** трёх типов: **sources**, **analyzers**, **outputs**. Ядро ничего не знает о конкретных источниках/анализаторах/выходах.

Второе архитектурное требование — **near-realtime event-driven pipeline** на **NATS JetStream** как единой внутренней шине. Без polling'а БД и без отдельной Kafka-инфраструктуры.

## Стек

- Python 3.13, **uv** (package + project manager, lock через `uv.lock`)
- FastAPI, SQLAlchemy 2.x (async), Alembic, asyncpg, pydantic-settings
- Postgres 17 (метаданные + FTS), Qdrant (векторы, 1024-dim под bge-m3)
- **NATS 2.10 + JetStream** (event bus)
- Ollama (bge-m3 embeddings + Qwen 2.5-14B generation)
- Telethon (Telegram), feedparser (RSS), `miniflux` Python SDK
- structlog, prometheus-client

---

## High-level architecture

```
┌──────────────┐          ┌──────────────────────┐          ┌──────────────┐
│  SOURCES     │          │   NATS JetStream     │          │  OUTPUTS     │
│  (adapters)  │          │   stream: homyak     │          │  (adapters)  │
│              │          │                      │          │              │
│ telegram     │          │  subjects:           │          │ FastAPI REST │
│  (via tg-    │─────────▶│   items.ingested     │─────────▶│ /feed.rss    │
│   relay)     │  publish │   items.processed    │ consumer │ /feed.json   │
│ miniflux     │          │   items.output       │   groups │ SSE stream   │
│ rss          │          │                      │          │ telegram-bot │
│ twitter      │          │  durable consumers:  │          │ cli / tui    │
│              │          │   processor          │          │              │
└──────┬───────┘          │   sse-broadcaster    │          └──────────────┘
       │                  │   tgbot-push         │                  ▲
       │ upsert           └──────────┬───────────┘                  │
       ▼                             │                              │
┌──────────────────────┐             │                              │
│  Postgres            │◀────────────┼──── update processing state  │
│  news_items          │             │                              │
│  clusters            │             │                              │
│  ingest_state        │             ▼                              │
└──────────┬───────────┘      ┌──────────────┐                      │
           │                  │  ANALYZERS   │                      │
           └────reads────────▶│  url_dedup   │──┐                   │
                              │  embedder    │  │                   │
                              │  sim_dedup   │  │ publish           │
                              │  llm_tagger  │  │ items.processed ──┘
                              │  llm_summary │  │
                              │  scorer      │  │
                              └──────┬───────┘  │
                                     │          │
                                     ▼          │
                              ┌──────────────┐  │
                              │  Qdrant      │  │
                              │  Ollama      │  │
                              └──────────────┘  │
```

---

## Event-driven pipeline (NATS JetStream)

**Stream**: `HOMYAK`
- Storage: `file` (JetStream FileStorage), retention `limits`, max_age 14d, max_bytes 5GB
- Replicas: 1 (personal-scale, single node)

**Subjects (3 штуки, один stream):**
- `homyak.items.ingested` — пушится source-адаптерами сразу после `upsert_item()`. Payload: `{news_item_id, source_type}`.
- `homyak.items.processed` — пушится processor'ом после прохождения всех analyzer-stages. Payload: `{news_item_id, cluster_id, category}`.
- `homyak.items.output` — опционально для сигнализации outputs (сейчас SSE/TG-bot достаточно processed).

**Durable consumers (consumer groups):**
- `processor` (on `items.ingested`) — pull consumer, `AckExplicit`, `MaxDeliver=5`, `AckWait=2m`. Несколько воркеров через одно consumer name делят нагрузку.
- `sse-broadcaster` (on `items.processed`) — push consumer, in-process, fan-out SSE-клиентам.
- `tgbot-push` (on `items.processed`) — push consumer, дергает TG Bot API для подписчиков.

**Почему JetStream, а не Kafka/Redis Streams:**
- Один 15MB бинарь, поднимается в docker-compose одной строкой.
- Durability + ack + replay — всё, что нужно от брокера.
- Native Python клиент `nats-py` с async/await.
- Kafka требует ZK/KRaft + стриминг-приложение; Redis Streams слабее по durability и consumer-семантике.

**Fallback sweep (безопасность)**: раз в 5 минут запускается sweep-джоб, который находит `news_items.processed_at IS NULL AND fetched_at < now() - 5min AND attempts < 5` и публикует `items.ingested` повторно. Это спасает от event loss при простое NATS/processor'а.

---

## Данные

### Postgres schema

```sql
CREATE TABLE news_items (
    id              bigserial PRIMARY KEY,
    source_type     text NOT NULL,
    source_id       text NOT NULL,
    url             text,
    title           text,
    text            text,
    media           jsonb NOT NULL DEFAULT '[]',
    author          text,
    raw_score       double precision,
    tags            text[] NOT NULL DEFAULT '{}',
    category        text,
    published_at    timestamptz,
    fetched_at      timestamptz NOT NULL DEFAULT now(),

    cluster_id      bigint REFERENCES clusters(id),
    embedding_model text,
    embedding_version int,
    processed_at    timestamptz,
    processing_started_at timestamptz,
    attempts        int NOT NULL DEFAULT 0,
    error           text,
    retry_after     timestamptz,

    search_tsv      tsvector GENERATED ALWAYS AS
                    (to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(text,''))) STORED,

    UNIQUE (source_type, source_id)
);

CREATE INDEX idx_news_unprocessed ON news_items (id) WHERE processed_at IS NULL;
CREATE INDEX idx_news_published   ON news_items (published_at DESC);
CREATE INDEX idx_news_cluster     ON news_items (cluster_id);
CREATE INDEX idx_news_fts         ON news_items USING gin (search_tsv);

CREATE TABLE clusters (
    id                bigserial PRIMARY KEY,
    representative_id bigint,
    size              int NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ingest_state (
    source_name text PRIMARY KEY,
    cursor      text,
    last_run_at timestamptz,
    last_error  text
);
```

### Qdrant
- Collection: `news_items`
- Vector size: 1024 (bge-m3), distance: Cosine
- Payload: `{news_item_id, source_type, published_at, category}`
- Точка создаётся в stage `embedder`; `embedding_version` в PG инкрементируется при смене модели → команда backfill переиндексирует.

### Идемпотентность
`(source_type, source_id)` UNIQUE. `upsert_item()` использует `ON CONFLICT … DO UPDATE` и возвращает `(id, was_new)`. `items.ingested` публикуется только при `was_new=true`.

---

## Plugin interfaces

`homyak/core/interfaces.py`:

```python
class PollSource(Protocol):
    name: str
    interval_seconds: int
    async def poll(self, cursor: str | None) -> AsyncIterator[tuple[NewsItem, str]]: ...

class PushSource(Protocol):
    name: str
    async def subscribe(self, sink: Callable[[NewsItem], Awaitable[None]]) -> None: ...

class Analyzer(Protocol):
    name: str
    stage: int
    async def analyze(self, item: NewsItem) -> AnalysisResult: ...

class OutputAdapter(Protocol):
    name: str
    async def serve(self, query: FeedQuery) -> Feed: ...
```

Разделение `PollSource` / `PushSource` принципиально: APScheduler крутит Poll'ы, long-running task крутит Push'и. Ядро выбирает стратегию по типу.

`FeedQuery`: `category`, `source_types`, `min_score`, `since`, `collapse_clusters`, `limit`.

---

## Directory layout

```
/Users/maks/projects/homyak/
  pyproject.toml                      # uv-managed, PEP 621
  uv.lock
  docker-compose.yml                  # postgres, qdrant, nats, ollama
  .env.example
  .python-version
  alembic.ini
  alembic/
    env.py                            # async engine
    versions/0001_initial.py
  docs/
    architecture.md                   # этот файл
    phase-1-skeleton.md               # задачи на этап скелета
    phase-2-*.md                      # появятся позже
  homyak/
    __init__.py
    core/
      models.py                       # SQLAlchemy + pydantic DTO
      interfaces.py                   # Protocol'ы
      config.py                       # pydantic-settings (.env + sources.yaml)
      registry.py                     # регистратор плагинов
      events.py                       # NATS publish helpers
    storage/
      postgres.py                     # upsert_item, save_cursor, …
      qdrant.py                       # upsert_vector, search_similar
    adapters/
      sources/
        miniflux.py
        rss.py
        telegram_relay.py
        twitter.py                    # stub, Phase 3
      analyzers/
        url_dedup.py
        embedder.py
        similarity_dedup.py
        llm_tagger.py
        llm_summarizer.py
        scorer.py
      outputs/
        api.py                        # FastAPI app
        rss_out.py                    # /feed.rss
        json_feed.py                  # /feed.json
        sse.py                        # /feed/stream (SSE)
        tg_bot.py                     # отдельный процесс
        cli.py                        # Textual TUI
    pipeline/
      ingest_poll.py                  # APScheduler runner для PollSource'ов
      tg_relay.py                     # tail outbox tscrapper'а → PG → publish
      processor.py                    # JetStream consumer "processor"
      sweeper.py                      # fallback re-publish для зависших items
      serve.py                        # uvicorn entrypoint
    config/
      sources.yaml
  tests/
    conftest.py
    test_*.py
```

---

## Source-by-source integration

### Telegram (через tscrapper)
tscrapper остаётся отдельным работающим сервисом. Патч в `_handle_message` (~30 строк) дописывает JSONL в локальный outbox. Homyak-процесс **`tg-relay`** тейлит outbox, валидирует pydantic'ом, делает `upsert_item`, `publish items.ingested`.

Причина outbox'а — decoupling durability. Падение Homyak-PG не ломает форвардинг tscrapper'а.

### Miniflux
REST API `/v1/entries?after_entry_id=<cursor>&limit=100&order=id&direction=asc`. Cursor = max `entry.id`, хранится в `ingest_state`. Interval 300s.

### RSS (generic)
`feedparser`, cursor = max `published_parsed` из прошлого poll'а. Конфиг в `config/sources.yaml`. Interval per-feed (60–600s).

### Twitter (Phase 3)
Напишем сами. Интерфейс `PushSource` через X API v2 filtered stream (realtime), либо `PollSource` с Nitter fallback. Внешняя оценка → `raw_score`.

---

## Processing pipeline stages

Analyzer'ы выполняются последовательно в `processor`'е (один consumer на `items.ingested`):

1. `url_dedup` — нормализует URL (утм, фрагмент), ищет cluster по нормализованному URL, joins / creates.
2. `embedder` — bge-m3 через Ollama `/api/embeddings`, **батч 32**, upsert точки в Qdrant, проставляет `embedding_model/version`.
3. `similarity_dedup` — Qdrant search top-5 с threshold 0.88 (эмпирически настраивается), merge clusters при попадании.
4. `llm_tagger` — Qwen 2.5-14B, top-5 тегов из фиксированного словаря + свободные.
5. `llm_summarizer` — 2-3 предложения на языке item'а.
6. `scorer` — `freshness * source_weight * cluster_size * (1 + raw_score)`.

После успеха: `processed_at = now()`, publish `items.processed`.

### Back-pressure & retry
- JetStream `MaxDeliver=5` + exponential backoff через `nak_delay`.
- Circuit breaker на Ollama: N ошибок подряд → pause consumer'а на 30s.
- Dead-letter subject `homyak.items.failed` (Phase 2.5) для замурованных item'ов.

---

## Конфигурация

`config/sources.yaml` — декларация источников (включенные RSS, интервалы, веса).
`.env` — секреты (`DATABASE_URL`, `MINIFLUX_URL`, `MINIFLUX_TOKEN`, `NATS_URL`, `QDRANT_URL`, `OLLAMA_URL`, `TELEGRAM_*`).

`pydantic-settings` читает `.env`, YAML — отдельным loader'ом (yaml + pydantic model). Секреты никогда не в YAML.

---

## Phasing

### Phase 1 — скелет
`docs/phase-1-skeleton.md`. Только `alembic upgrade head` работает против поднятого Postgres. Без адаптеров, без pipeline.

### Phase 2 — MVP feedthrough
- `core/interfaces.py`, `core/events.py` (NATS publish)
- `storage/postgres.py` (repo: upsert, cursor)
- `adapters/sources/miniflux.py` + `rss.py`
- `adapters/analyzers/url_dedup.py`
- `pipeline/processor.py` с JetStream consumer
- `adapters/outputs/api.py` + `rss_out.py` + `json_feed.py`
- FastAPI `/feed`, `/item/{id}`, `/feed.rss`, `/feed.json`, `/healthz`

### Phase 3 — embeddings + Telegram + SSE
- Патч tscrapper'а + `pipeline/tg_relay.py`
- `storage/qdrant.py`, `adapters/analyzers/embedder.py`, `similarity_dedup.py`
- `adapters/outputs/sse.py` (JetStream → SSE)

### Phase 4 — LLM + бот + CLI
- `llm_tagger.py`, `llm_summarizer.py`, `scorer.py`
- `adapters/outputs/tg_bot.py`, `cli.py`

### Phase 5 — Twitter + Web UI
- `adapters/sources/twitter.py` (filtered stream)
- React/Vite фронт поверх `/api` + SSE
- LLM-скоринг с user feedback

---

## Key decisions

- **uv, не poetry** — скорость, single binary, современный resolver. `pyproject.toml` PEP 621, `uv.lock` в git.
- **NATS JetStream, не Kafka/Redis** — минимум инфры, всё нужное есть, single binary.
- **Postgres + Qdrant (не pgvector)** — заложились на рост и payload-фильтры Qdrant'а.
- **`(source_type, source_id)` UNIQUE, не URL** — один URL приходит из нескольких источников → отдельные item'ы склеиваются в один `cluster_id`.
- **Event-driven через JetStream, не polling `processed_at IS NULL`** — real-time latency ~ms, fan-out нескольким consumer'ам.
- **Fallback sweep оставляем** — NATS может простоять, sweep поднимет tail.
- **tscrapper через outbox JSONL** — durability decoupling, tscrapper независим от Homyak-PG.
- **Cluster_id soft-reassignable** — защита от false-positive similarity (admin endpoint для ручного split).
- **Embedding model versioning** — `embedding_model/version` в PG, backfill command при смене модели.

---

## Failure modes (кратко)

| Сценарий | Поведение |
|---|---|
| NATS упал | Ingest продолжает писать в PG, publish логирует ошибку. Sweeper переопубликует при возврате NATS. |
| Postgres упал | Ingest падает, tscrapper продолжает форвардить (outbox копится). TG-relay наверстает. |
| Ollama упал | Processor маркирует `attempts++`, circuit breaker на 30s, fallback на `items.ingested` через JetStream retry. |
| Consumer zombie | JetStream `AckWait=2m` возвращает сообщение другому инстансу. |
| Дубль при ingest | `ON CONFLICT DO UPDATE`, publish только при `was_new=true`. |
| Qdrant не соответствует PG | Backfill command: идёт по `news_items WHERE embedding_version < current`. |

---

## Verification (после Phase 2)

```bash
docker compose up -d postgres qdrant nats
uv sync
uv run alembic upgrade head
uv run homyak-ingest-poll &
uv run homyak-processor &
uv run homyak-api
curl localhost:8000/feed | jq '.items | length'
curl localhost:8000/feed.rss | xmllint --format -
nats stream info HOMYAK
nats consumer info HOMYAK processor
```
