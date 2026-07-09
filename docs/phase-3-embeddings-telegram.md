# Phase 3 — Embeddings, Telegram, SSE

## Цель

Три добавки к Phase 2 MVP:

1. **Telegram-источник** через минимальный патч в `tscrapper` (локальный outbox JSONL) + Homyak-процесс `tg-relay`, тейлящий outbox → upsert в PG → publish NATS.
2. **Эмбеддинги + similarity-дедупликация** — bge-m3 через Ollama, векторы в Qdrant, pipeline-анализатор `similarity_dedup` мержит кластеры по cosine threshold.
3. **Server-Sent Events output** — FastAPI endpoint `/feed/stream`, который подписан на JetStream `items.processed` и пушит новые events клиентам в реальном времени.

Успех = новости из Telegram-каналов tscrapper'а попадают в общую ленту, одна и та же новость из TG и RSS склеивается в один cluster_id по similarity, SSE-клиент получает события через ~секунду после `mark_processed`.

## Prerequisites

- Phase 2 завершена: PG + NATS + ingest-poll + processor + API работают.

## Что в scope

- docker-compose: раскомментировать `ollama`, оставить `qdrant`
- Qdrant setup: создание коллекции `news_items` (dim=1024, Cosine)
- Pull моделей: `ollama pull bge-m3`, `ollama pull qwen2.5:14b-instruct` (qwen нужен с Phase 4, но тянем заранее)
- Патч `tscrapper` — outbox JSONL append в `_handle_message`
- `homyak/storage/qdrant.py` — upsert_vector, search_similar, delete_vector
- `homyak/adapters/analyzers/embedder.py` — bge-m3 через Ollama, батчинг 32
- `homyak/adapters/analyzers/similarity_dedup.py` — Qdrant search, merge clusters
- `homyak/adapters/sources/telegram_relay.py` — PushSource, тейлит outbox JSONL
- `homyak/pipeline/tg_relay.py` — entrypoint: запускает telegram_relay, wires в repo + bus
- `homyak/adapters/outputs/sse.py` — FastAPI SSE endpoint, JetStream `items.processed` → EventStream
- Миграция 0003 — `news_items.url_normalized` unique-per-cluster тригер (опционально), cleanup nullability
- Backfill-команда: `homyak-reembed` — пройти по items с `embedding_version < current`, переэмбеддить

## Что НЕ в scope

- LLM tagger / summarizer — Phase 4
- Telegram-бот (output) — Phase 4
- CLI/TUI — Phase 4
- Twitter — Phase 5
- Web UI — Phase 5

---

## Компоненты

### 1. Ollama setup

**docker-compose.yml** — раскомментировать:
```yaml
ollama:
  image: ollama/ollama:latest
  ports: ["11434:11434"]
  volumes: [ollama_data:/root/.ollama]
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
  # На Mac (без nvidia) блок deploy.resources убирается — Ollama сама использует Metal
```

**Init**:
```bash
docker compose up -d ollama
docker compose exec ollama ollama pull bge-m3
docker compose exec ollama ollama pull qwen2.5:14b-instruct  # для Phase 4
```

**`.env`** добавляется:
```env
OLLAMA_URL=http://localhost:11434
EMBEDDING_MODEL=bge-m3
EMBEDDING_VERSION=1
EMBEDDING_BATCH_SIZE=32
```

### 2. Qdrant setup

**Init collection** — idempotent в `storage/qdrant.py`:
- `QdrantStore.__init__(url)` — коннект
- `async def ensure_collection()` — если `news_items` не существует, create с `size=1024, distance=Cosine`, `hnsw_config` default, `payload_schema` для `source_type`, `category`, `published_at`
- Вызывается на старте processor'а

**`homyak/storage/qdrant.py`**:

```python
class QdrantStore:
    COLLECTION = "news_items"

    async def ensure_collection(self) -> None: ...
    async def upsert_vector(self, news_item_id: int, vector: list[float], payload: dict) -> None: ...
    async def search_similar(self, vector: list[float], limit: int = 5, score_threshold: float = 0.88) -> list[tuple[int, float]]: ...
    async def delete_vector(self, news_item_id: int) -> None: ...
    async def batch_upsert(self, points: list[tuple[int, list[float], dict]]) -> None: ...
```

### 3. Embedder analyzer

**`homyak/adapters/analyzers/embedder.py`**:
- `stage = 2`
- Per-item интерфейс `analyze(item_id, session)` невыгоден — каждый POST в Ollama с одним item'ом = ~20ms overhead. Решение — **батчинг на уровне processor'а**: processor копит items, запускает `embedder.analyze_batch([...])` раз в 200ms или при наборе 32 items.
- **Протокол расширяется**: добавляем `BatchAnalyzer(Analyzer)` с `async def analyze_batch(items: list[tuple[int, NewsItem]]) -> list[AnalysisResult]`. Processor детектит по isinstance и переключается на батч-режим.
- Prompt'а нет — просто embeddings. Текст для эмбеддинга = `title + "\n" + text[:2000]` (обрезаем чтобы не упереться в context).
- Ollama endpoint: `POST /api/embeddings {model: "bge-m3", prompt: "..."}`. Для батча — отдельный запрос на каждый (Ollama пока не поддерживает multi-input embeddings в одном запросе), но запускаем параллельно через `asyncio.gather`. По мере появления поддержки — переход на `POST /api/embed {input: [...]}`.
- Каждый вектор → `qdrant.upsert_vector(news_item_id, vector, payload)`, проставляем `news_items.embedding_model = settings.embedding_model`, `embedding_version = settings.embedding_version`.
- Возвращает `AnalysisResult(embedding=None)` (вектор уже в Qdrant, в PG хранить не нужно).

### 4. Similarity dedup analyzer

**`homyak/adapters/analyzers/similarity_dedup.py`**:
- `stage = 3` (после embedder)
- `analyze(item_id, session)`:
  1. Получаем vector из Qdrant (or держим в locals после embedder'а — стадии связаны через shared context в processor'е, см. ниже)
  2. `hits = await qdrant.search_similar(vector, limit=5, score_threshold=0.88)` — исключая сам item_id
  3. Если `hits` пустой → возвращаем текущий `cluster_id` (как есть)
  4. Если есть → берём лучший cluster из hits (по score desc), merge current cluster в найденный:
     - UPDATE `news_items` SET `cluster_id = found_cluster_id` WHERE `cluster_id = current_cluster_id`
     - UPDATE `clusters` SET `size = size + ...` у найденного, удалить/nullify old cluster
  5. Возврат `AnalysisResult(cluster_id=found_cluster_id)`
- **Threshold 0.88** — стартовый; корректируем через A/B на реальных данных. Хранится в config.
- **Race condition protection**: UPDATE внутри единой транзакции + advisory lock `pg_advisory_xact_lock(found_cluster_id)` чтобы 2 processor'а не мержили одновременно одинаково и не создавали противоречий.
- **Soft reassignable**: admin endpoint `/admin/clusters/{id}/split` умеет разжоинить item в отдельный cluster (для false-positives).

### 5. Shared analyzer context

**Изменение в processor'е**: analyzer'ы делятся данными через **AnalyzerContext** (аналог request-local):

```python
@dataclass
class AnalyzerContext:
    item_id: int
    item: NewsItem
    session: AsyncSession
    # прокидывается между analyzer'ами:
    embedding: list[float] | None = None
    cluster_id: int | None = None
    tags: list[str] | None = None
    summary: str | None = None
    score: float | None = None
```

Embedder пишет `ctx.embedding`; similarity_dedup читает `ctx.embedding` вместо повторного запроса в Qdrant. Protocol `Analyzer` меняется на `async def analyze(ctx: AnalyzerContext) -> None`, AnalysisResult упраздняется (вместо него мутация ctx).

### 6. Telegram integration

#### Патч tscrapper

**`/Users/maks/projects/tscrapper/tscraper.py`** — в `_handle_message` после успешного forward:

```python
# pseudo:
async def _handle_message(self, event):
    # ... existing forward logic ...
    try:
        await self._outbox_append({
            "source_type": "telegram",
            "source_id": f"{event.chat_id}:{event.message.id}",
            "url": self._permalink(event),
            "title": None,  # TG не имеет title
            "text": event.message.text or event.message.raw_text or "",
            "media": [...],  # file_id список для фото/видео/документов
            "author": str(event.chat.username or event.chat.id),
            "category": self._route_to_category(event),  # news_ai / news_tech
            "published_at": event.message.date.isoformat(),
        })
    except Exception as e:
        logger.warning("outbox_append_failed", error=str(e))
        # НЕ падаем — форвардинг важнее хранения
```

**Outbox**: JSONL файл `/var/lib/tscrapper/outbox.jsonl`:
- Append-only, fsync после каждой записи (для durability)
- Rotation по размеру (e.g. >100MB → `outbox-YYYYMMDD.jsonl.gz`)
- Permission: tscrapper пишет, `tg-relay` читает + усекает после подтверждения offset'а

**Изменение в tscrapper**: выносим outbox путь в config.yaml как опциональный `outbox_path: /var/lib/tscrapper/outbox.jsonl`. Если не задан — outbox отключён.

#### TG relay

**`homyak/adapters/sources/telegram_relay.py`** (PushSource):
- `name = "telegram-relay"`
- Тейлит outbox:
  - Открываем файл в режиме чтения, seek на offset из `ingest_state.cursor` (byte offset)
  - `while True`: читаем doступные строки, для каждой строки:
    - `item = TelegramOutboxLine.parse_raw(line)` (pydantic валидация)
    - `await sink(item)` — передаём в pipeline (который делает upsert + publish)
  - `sleep(0.5)`, проверяем rotation (если файл уменьшился — резетим offset)
  - Сохраняем byte offset в `ingest_state` после каждых N строк

**`homyak/pipeline/tg_relay.py`** — entrypoint:
- Инстанцирует `TelegramRelaySource`, `NewsRepo`, `NatsBus`
- `await source.subscribe(handler)` где `handler = compose(repo.upsert_item, bus.publish_ingested_if_new)`
- Graceful shutdown с сохранением offset

**Идемпотентность**: `(source_type='telegram', source_id='chat:msg')` UNIQUE гарантирует, что даже если relay упадёт и перечитает outbox, дублей в PG не будет.

### 7. SSE output

**`homyak/adapters/outputs/sse.py`**:
- FastAPI endpoint `GET /feed/stream`
- Accept params: `category`, `source_types`, `min_score` (фильтры на события)
- Логика:
  1. Открываем StreamingResponse с mimetype `text/event-stream`
  2. Создаём ephemeral JetStream consumer на `homyak.items.processed` (`deliver_policy=new`, нет durable'а — per-connection)
  3. Бесконечный loop: `msg = await sub.next_msg(timeout=30)`
     - timeout → yield `: keepalive\n\n`
     - msg → parse `{news_item_id, cluster_id, category}`, применяем фильтры; если проходит — берём item из PG, формируем `data: {...}\n\n`
     - `msg.ack()`
  4. При cancel/disconnect: закрываем consumer

**Пример клиента**:
```js
const es = new EventSource('/feed/stream?category=ai');
es.onmessage = (e) => console.log(JSON.parse(e.data));
```

**`homyak/adapters/outputs/api.py`** добавляет роут `app.include_router(sse.router)`.

### 8. Backfill command

**`homyak/cli/reembed.py`** — standalone script (не analyzer):
- Запускается вручную при смене модели: `uv run homyak-reembed`
- Логика:
  1. SELECT id, title, text FROM news_items WHERE `embedding_version < settings.embedding_version` OR `embedding_model != settings.embedding_model`
  2. Батчами по 32:
     - Эмбеддим, upsert в Qdrant, UPDATE PG
  3. Progress bar через `rich` или `tqdm`
- Entry point: `homyak-reembed = "homyak.cli.reembed:main"`

### 9. Circuit breaker для Ollama

**`homyak/core/circuit.py`**:
- Простой async circuit breaker: N ошибок подряд → `state=open` на 30s → `state=half_open` → try → `closed`
- Wraps `embedder.embed_batch`. При `open` — `raise ServiceUnavailable` → processor делает `nak(delay=30s)`.

### 10. Новые зависимости

К Phase 2 добавляется:
- `qdrant-client>=1.10`
- `sse-starlette>=2.0` (для SSE в FastAPI)
- `rich>=13.0` (для backfill CLI)

### 11. Миграция 0003

- `news_items.embedding_model` и `embedding_version` **уже есть с Phase 1** — миграции не нужны на них.
- Индекс `idx_news_embedding_version` для быстрого scan backfill'ом: `CREATE INDEX idx_news_embedding_version ON news_items(embedding_version) WHERE embedding_version IS NOT NULL`.

---

## Acceptance criteria

```bash
# 1. Ollama + Qdrant
docker compose up -d ollama qdrant
docker compose exec ollama ollama pull bge-m3
docker compose exec ollama ollama pull qwen2.5:14b-instruct

# 2. Миграция 0003
uv run alembic upgrade head

# 3. tscrapper outbox (отдельно, в репо tscrapper'а)
cd /Users/maks/projects/tscrapper
# применяем патч + перезапускаем
poetry run tscraper &

# 4. TG relay + обновлённый processor в Homyak
cd /Users/maks/projects/homyak
uv run homyak-tg-relay &

uv run homyak-processor  # перезапуск с новыми analyzer'ами

# 5. Провокация: отправить в отслеживаемый TG-канал новость, дубликат которой есть в RSS
#    (напр. TechCrunch заметка → TG AI News → через несколько минут Homyak должен склеить)

# 6. Проверки

# Telegram попал в PG
psql ... -c "SELECT source_type, count(*) FROM news_items GROUP BY 1"
# Видим telegram строки

# Qdrant наполняется
curl -s localhost:6333/collections/news_items | jq '.result.points_count'
# > 0

# Similarity merge случился
psql ... -c "
SELECT cluster_id, array_agg(source_type) AS sources, count(*) AS n
FROM news_items GROUP BY cluster_id HAVING count(*) > 1 AND 'telegram' = ANY(array_agg(source_type)) AND 'rss' = ANY(array_agg(source_type))
"
# Хотя бы один cluster с TG+RSS

# SSE работает
curl -N localhost:8000/feed/stream
# Появляются data: {...} события по мере processing'а

# Backfill
uv run homyak-reembed
# Проходит без ошибок, Qdrant count = news_items.count
```

## Checklist

- [ ] Ollama поднят, bge-m3 скачан, `curl localhost:11434/api/embeddings -d '{"model":"bge-m3","prompt":"hi"}'` возвращает 1024-dim vector
- [ ] Qdrant поднят, коллекция `news_items` создана с dim=1024, Cosine
- [ ] `storage/qdrant.py` покрыт тестами (upsert, search, delete)
- [ ] `analyzers/embedder.py` реализует BatchAnalyzer, батч 32
- [ ] Circuit breaker срабатывает при 5 подряд ошибках Ollama
- [ ] `analyzers/similarity_dedup.py` с advisory lock, threshold 0.88, корректный merge clusters
- [ ] AnalyzerContext пробрасывается между stage'ами
- [ ] tscrapper outbox JSONL append работает, не ломает форвардинг при ошибке I/O
- [ ] `sources/telegram_relay.py` тейлит, сохраняет offset, переживает rotation
- [ ] Duplicate TG outbox → один item в PG (идемпотентность)
- [ ] SSE endpoint отдаёт events через < 2s после publish_processed
- [ ] Backfill-команда переэмбеддит старые items без дублей
- [ ] Миграция 0003 применяется и откатывается
