# Homyak — архитектура решения

## Назначение

Персональный агрегатор новостей, объединяющий разнородные источники (Telegram-каналы через tscrapper→NATS, Miniflux RSS, новостные сайты по RSS) в единую дедуплицированную **персональную** ленту с локальной LLM-обработкой. Главный выход — **Telegram-бот** с ранжированием под интересы, реакциями 👍/👎 (обучение), саммари и читалкой полного текста (Telegraph). Плюс CLI, REST/RSS/JSON feed, SSE.

> Статус: реализованы Phase 1-4 + вся персонализация (Phase 6.0-6.3). Весь стек контейнеризован под **Podman**. Подробности фич — в `docs/phase-*.md`.

Ключевое архитектурное требование — **плагинная система адаптеров** трёх типов: **sources**, **analyzers**, **outputs**. Ядро ничего не знает о конкретных источниках/анализаторах/выходах.

Второе архитектурное требование — **near-realtime event-driven pipeline** на **NATS JetStream** как единой внутренней шине. Без polling'а БД и без отдельной Kafka-инфраструктуры.

## Стек

- Python 3.13, **uv** (package + project manager, lock через `uv.lock`)
- FastAPI, SQLAlchemy 2.x (async), Alembic, asyncpg, pydantic-settings
- Postgres 17 (метаданные + FTS), Qdrant (векторы, 1024-dim под bge-m3, коллекции `news_items` + `taste`)
- **NATS 2.10 + JetStream** (event bus)
- **Ollama** (на хосте, Metal): `bge-m3` (эмбеддинги), `qwen2.5:14b` (судья/теги), `gpt-oss:120b-cloud`+`gemma4` (саммари)
- feedparser + trafilatura (скачивание/извлечение полного текста статьи), httpx; aiogram (бот); Telethon в tscrapper
- structlog
- **Деплой:** `Dockerfile` + `docker-compose.yml` (podman compose) — postgres/qdrant/nats + migrate +
  app-сервисы (ingest-poll, telegram-ingest, processor, learner, sweeper, tgbot, api). Ollama — на хосте
  (`host.containers.internal:11434`). `podman compose up -d` поднимает всё.

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

**Subjects (актуальные):**
- `homyak.items.ingested` — от source-адаптеров/консюмеров после `upsert_item()`. Payload: `{news_item_id, source_type}`.
- `homyak.items.processed` — от processor'а после всех analyzer-stages. Payload: `{news_item_id, cluster_id, category}`.
- `homyak.telegram.raw` — сырые сообщения от **tscrapper** (доработан: публикует в NATS). Консюмер `telegram-ingest` делает upsert → `items.ingested`.
- `homyak.feedback.recorded` — реакции 👍/👎/⭐/🔇 из бота. Консюмер `learner` двигает вкус/веса.
- `homyak.profile.suggestion` — предложение правки профиля (от learner раз в N фидбеков) → бот показывает карточку.

**Durable consumers:** `processor` (on `items.ingested`, pull, ack/nak+backoff, max_deliver=5), `learner` (on `feedback.recorded`), `telegram-ingest` (on `telegram.raw`), `profile-suggest` (бот, on `profile.suggestion`); TG-бот push и SSE — ephemeral consumers на `items.processed` (deliver=new).

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

Analyzer'ы выполняются последовательно в `processor`'е (мутируют общий `AnalyzerContext`), **актуальная цепочка из 9 стадий**:

0. `article_fetch` — **скачивает полный текст статьи по URL** (trafilatura), если RSS дал огрызок (HN → пусто → полная статья). Отдельный компонент `core/article.py`.
1. `url_dedup` — нормализует URL, ищет/создаёт cluster по нормализованному URL.
2. `embedder` — bge-m3 через Ollama `/api/embed`, upsert в Qdrant, проставляет `embedding_model/version`.
3. `similarity_dedup` — Qdrant search top-5, threshold 0.88, merge clusters (advisory-lock).
4. `llm_tagger` — теги из словаря (языки/системщина/AI) + свободные, `qwen2.5:14b`, JSON.
5. `llm_summarizer` — «микс»-саммари (живой гист + «что вынесешь») голосом инженера, на языке оригинала. **`gpt-oss:120b-cloud` → fallback `gemma4`**.
6. `scorer` — базовый `freshness · (1+raw) · (1+ln(cluster_size))` (в Phase 6 заменён personal_score'ом).
7. `llm_relevance` — **LLM-судья**: релевантность профилю интересов (0..1) + reason, кэш по `scored_profile_version`.
8. `personalizer` — гибридная свёртка `personal_score` (llm + taste + tag/source affinity + fresh) − hard-mute.

После успеха: `processed_at = now()`, publish `items.processed`.

### Back-pressure & retry
- JetStream `MaxDeliver=5` + exponential backoff через `nak_delay`.
- Circuit breaker на Ollama: N ошибок подряд → pause consumer'а на 30s.
- Dead-letter subject `homyak.items.failed` (Phase 2.5) для замурованных item'ов.

---

## Тематические вертикали (business / it / medical)

Лента разделена на **3 независимые вертикали** — у каждой свой профиль интересов, своё обучение,
свой вектор вкуса и своя лента. Лайк в IT не влияет на medical.

- **Классификация:** `llm_tagger` возвращает `vertical` (business/it/medical/other) в JSON. `other`
  не попадает ни в одну вертикаль (`personal_score = NULL`).
- **Per-vertical состояние** (миграция 0007): `news_items.vertical`; таблицы `profile`, `tag_affinity`,
  `source_affinity`, `taste_state` ключуются по вертикали (один активный профиль на вертикаль);
  вектор вкуса — своя точка в Qdrant-коллекции `taste` на каждую вертикаль.
- **Скоринг:** `llm_relevance` судит статью против профиля ЕЁ вертикали; `personalizer` считает
  `personal_score` из аффинити/вкуса этой вертикали. `learner` обучает вертикаль, к которой относится item.
- **Профили:** `config/profiles/{business,it,medical}.yaml` → `homyak-profile-set`. Правки профилей и
  рефайнмент — per-vertical.
- **Выходы:** бот — команды/кнопки `/business /it /medical`, метка вертикали в посте; лента `?vertical=`.
- **Источники** тематические (WSJ/Economist… → business; STAT/Lancet… → medical; HN/arxiv… → it), но
  вертикаль определяет теггер по содержимому, не источник.

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

### Phase 3 — embeddings + Telegram + SSE  ✅ (реализовано)
- `storage/qdrant.py`, `adapters/analyzers/embedder.py` (bge-m3), `similarity_dedup.py`
- `adapters/outputs/sse.py` (JetStream → SSE), `core/circuit.py`, `cli/reembed.py`
- **Telegram через NATS (2026-07-12):** tscrapper доработан — `_handle_message` публикует каждое
  сообщение в NATS subject `homyak.telegram.raw` (best-effort). Homyak: `pipeline/telegram_ingest`
  (`homyak-telegram-ingest`) консюмит → upsert → `items.ingested`. Файловый `telegram_relay`/`tg_relay`
  остаётся как альтернатива, но основной путь — NATS.

### Phase 4 — LLM + бот + CLI  ◑ (анализаторы+CLI готовы; бот ждёт токен)
`docs/phase-4-llm-bot-cli.md`. `llm_tagger`/`llm_summarizer`/`scorer` (qwen2.5:14b), `core/llm.py`,
`core/scoring.py`, миграция 0004 (summary/score). `adapters/outputs/cli.py` готов;
`tg_bot.py` — после токена от @BotFather.

### Phase 5 — Twitter + Web UI
- `adapters/sources/twitter.py` (filtered stream)
- React/Vite фронт поверх `/api` + SSE

### Phase 6 — Персонализация (флагманская фича)
`docs/phase-6-personalization.md`. Гибридный ранкер под интересы: LLM-судья против профиля +
обучение на 👍/👎 из Telegram-бота (taste vector + tag/source affinity). Заменяет наивный
`scorer` на `personal_score`. Зависит от Phase 3 (эмбеддинги) и Phase 4 (LLM); Phase 5 не нужна.

- **6.0 offline-скоринг ✅** — миграция 0005 (profile/tag_affinity/source_affinity/feedback/taste_state +
  колонки), `core/scoring.py::personal_score`, `llm_relevance` (stage 7, судья), `personalizer`
  (stage 8, свёртка + hard-mute), `homyak-profile-set`. Лента `?sort=personal`. E2E: AI/Rust
  ранжируются высоко, спорт низко, крипта отсекается mute. (В доке миграция названа 0004 — фактически 0005.)
- **6.1-6.3 (осталось)** — бот-реакции 👍/👎 + `learner` (обучение taste/affinity), политика пуша,
  команды бота, profile refinement. Требуют TG-бот (токен от @BotFather).

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
