# Phase 1 — Скелет проекта

## Цель

Минимально работающий каркас: поднять **Postgres** через `docker compose`, применить Alembic-миграцию и увидеть созданные таблицы. **Никаких адаптеров, pipeline'а и API на этом этапе нет** — это валидация структуры проекта и миграционной инфраструктуры.

Успех = команда `uv run alembic upgrade head` проходит без ошибок и создаёт схему из [`architecture.md`](architecture.md) в Postgres.

## Что в scope

- Конфигурация пакета через `uv` (PEP 621 `pyproject.toml` + `uv.lock`)
- `docker-compose.yml` с тремя сервисами: `postgres`, `qdrant`, `nats` (на Phase 1 активно используется только postgres; qdrant/nats поднимаем сразу, чтобы образы скачались и compose был готов к Phase 2)
- Alembic с async engine
- Первая миграция `0001_initial.py` — `news_items`, `clusters`, `ingest_state`, индексы, FTS tsvector, partial index для `processed_at IS NULL`
- `homyak/core/models.py` — SQLAlchemy declarative для `alembic --autogenerate` в будущих миграциях
- `homyak/core/config.py` — pydantic-settings, загружает `DATABASE_URL` из `.env`
- `.env.example`, `.gitignore`, `.python-version`

## Что НЕ в scope (явно)

- Source/analyzer/output адаптеры
- NATS publish/consume код (образ в compose, но не используется)
- FastAPI routes
- tscrapper patch
- pydantic DTO `NewsItem` (только SQLAlchemy)
- Тесты (появятся в Phase 2)
- Ollama-образ в compose — раскомментируем в Phase 3

---

## Задачи

### 1. Инициализация uv-проекта

**Файл**: `/Users/maks/projects/homyak/pyproject.toml`

- `[project]` секция: `name = "homyak"`, `version = "0.1.0"`, `requires-python = ">=3.13"`
- Минимальные dependencies:
  - `sqlalchemy[asyncio]>=2.0`
  - `alembic>=1.13`
  - `asyncpg>=0.29`
  - `pydantic>=2.6`
  - `pydantic-settings>=2.2`
  - `python-dotenv>=1.0`
  - `structlog>=24.1`
- `[build-system]` hatchling → packages `["homyak"]`
- `[project.scripts]` пусто до Phase 2

**Файл**: `/Users/maks/projects/homyak/.python-version` → `3.13`

### 2. Docker compose

**Файл**: `/Users/maks/projects/homyak/docker-compose.yml`

Сервисы:
- `postgres` — `postgres:17-alpine`
  - Порт `5432`, volume `pgdata`
  - Env: `POSTGRES_USER=homyak`, `POSTGRES_PASSWORD=homyak`, `POSTGRES_DB=homyak`
  - Healthcheck: `pg_isready -U homyak`
- `qdrant` — `qdrant/qdrant:latest`
  - Порты `6333` (REST), `6334` (gRPC), volume `qdrant_storage`
- `nats` — `nats:2.10-alpine`
  - Порты `4222` (client), `8222` (monitoring)
  - Command: `["-js", "-sd", "/data", "-m", "8222"]`
  - Volume `nats_data`
- `ollama` — **закомментирован**, включаем в Phase 3 (образ `ollama/ollama`, порт `11434`, volume `ollama_data`)

### 3. Переменные окружения

**Файл**: `/Users/maks/projects/homyak/.env.example`

```env
DATABASE_URL=postgresql+asyncpg://homyak:homyak@localhost:5432/homyak
# Задаём сразу, в Phase 1 не используются:
QDRANT_URL=http://localhost:6333
NATS_URL=nats://localhost:4222
OLLAMA_URL=http://localhost:11434
```

**Файл**: `/Users/maks/projects/homyak/.gitignore`

```
.env
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
# uv.lock коммитим
```

### 4. Пакет homyak (минимум)

**Файл**: `/Users/maks/projects/homyak/homyak/__init__.py` — пустой.

**Файл**: `/Users/maks/projects/homyak/homyak/core/__init__.py` — пустой.

**Файл**: `/Users/maks/projects/homyak/homyak/core/config.py`

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = Field(alias="DATABASE_URL")

settings = Settings()
```

**Файл**: `/Users/maks/projects/homyak/homyak/core/models.py`

SQLAlchemy 2.x declarative. Postgres-диалект, используем все целевые типы:
- `Base = DeclarativeBase`
- **Типы**: `BigInteger` для PK (`Identity(...)`), `String`, `Text`, `sqlalchemy.dialects.postgresql.JSONB` для `media`, `ARRAY(Text)` для `tags`, `DateTime(timezone=True)`, `Float`, `TSVECTOR` для `search_tsv` (Computed из `title + text`)
- `NewsItem`:
  - `id: BigInteger, primary_key=True, Identity(always=False)`
  - `source_type: String(32), nullable=False`
  - `source_id: String(255), nullable=False`
  - `url: Text, nullable=True`
  - `title: Text, nullable=True`
  - `text: Text, nullable=True`
  - `media: JSONB, nullable=False, default=list`
  - `author: String(255), nullable=True`
  - `raw_score: Float, nullable=True`
  - `tags: ARRAY(Text), nullable=False, default=list`
  - `category: String(64), nullable=True`
  - `published_at: DateTime(timezone=True), nullable=True`
  - `fetched_at: DateTime(timezone=True), nullable=False, server_default=func.now()`
  - `cluster_id: BigInteger, FK → clusters.id, nullable=True`
  - `embedding_model: String(64), nullable=True`
  - `embedding_version: Integer, nullable=True`
  - `processed_at: DateTime(timezone=True), nullable=True`
  - `processing_started_at: DateTime(timezone=True), nullable=True`
  - `attempts: Integer, nullable=False, server_default="0"`
  - `error: Text, nullable=True`
  - `retry_after: DateTime(timezone=True), nullable=True`
  - `search_tsv: TSVECTOR, Computed("to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(text,''))", persisted=True)`
  - `UniqueConstraint("source_type", "source_id")`
  - Индексы:
    - `Index("idx_news_unprocessed", "id", postgresql_where=text("processed_at IS NULL"))`
    - `Index("idx_news_published", text("published_at DESC"))`
    - `Index("idx_news_cluster", "cluster_id")`
    - `Index("idx_news_fts", "search_tsv", postgresql_using="gin")`
- `Cluster`:
  - `id: BigInteger PK Identity`
  - `representative_id: BigInteger, FK → news_items.id, nullable=True, use_alter=True`
  - `size: Integer, nullable=False, server_default="1"`
  - `created_at: DateTime(timezone=True), nullable=False, server_default=func.now()`
- `IngestState`:
  - `source_name: String(255), primary_key=True`
  - `cursor: Text, nullable=True`
  - `last_run_at: DateTime(timezone=True), nullable=True`
  - `last_error: Text, nullable=True`

### 5. Alembic

**Файл**: `/Users/maks/projects/homyak/alembic.ini`

Стандартный:
- `script_location = alembic`
- `sqlalchemy.url =` (пусто, подхватим из env в `env.py`)

**Файл**: `/Users/maks/projects/homyak/alembic/env.py`

Async-env.py:
- Импорт `settings` из `homyak.core.config` и `Base.metadata` из `homyak.core.models`
- `target_metadata = Base.metadata`
- `run_async_migrations()` через `async_engine_from_config` + `connection.run_sync(...)`
- URL подставляется из `settings.database_url`

**Файл**: `/Users/maks/projects/homyak/alembic/versions/0001_initial.py`

Ручная миграция (не autogenerate — контролируем порядок создания и postgres-specific конструкции):
- `op.create_table("clusters", ...)` — без FK `representative_id` на этом шаге (ещё нет `news_items`)
- `op.create_table("news_items", ...)` с `search_tsv` через `sa.Computed(..., persisted=True)`, UNIQUE `(source_type, source_id)`, FK `cluster_id → clusters.id`
- `op.create_table("ingest_state", ...)`
- Индексы:
  - `op.create_index("idx_news_unprocessed", "news_items", ["id"], postgresql_where=sa.text("processed_at IS NULL"))`
  - `op.create_index("idx_news_published", "news_items", [sa.text("published_at DESC")])`
  - `op.create_index("idx_news_cluster", "news_items", ["cluster_id"])`
  - `op.create_index("idx_news_fts", "news_items", ["search_tsv"], postgresql_using="gin")`
- `op.create_foreign_key("fk_cluster_repr", "clusters", "news_items", ["representative_id"], ["id"], ondelete="SET NULL")` — после создания обеих таблиц
- `downgrade()` симметричный (drop FK → drop indexes → drop tables)

### 6. Acceptance script

**Файл**: `/Users/maks/projects/homyak/scripts/verify-phase-1.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d postgres
# ждём healthcheck
until docker compose exec -T postgres pg_isready -U homyak >/dev/null 2>&1; do sleep 0.5; done

cp -n .env.example .env || true
uv sync
uv run alembic upgrade head

psql "postgresql://homyak:homyak@localhost:5432/homyak" -c '\dt'
# Ожидаем: alembic_version  clusters  ingest_state  news_items

psql "postgresql://homyak:homyak@localhost:5432/homyak" -c '\d news_items' | head -30
```

---

## Checklist

- [ ] `pyproject.toml` создан, `uv sync` проходит, `.venv/` появляется, `uv.lock` генерится
- [ ] `.python-version` = `3.13`, `.env.example`, `.gitignore` в репо
- [ ] `docker-compose.yml` создан, `docker compose config` валиден
- [ ] `docker compose up -d postgres` поднимает контейнер, `pg_isready` → OK
- [ ] `homyak/core/config.py` импортируется (`uv run python -c "from homyak.core.config import settings; print(settings.database_url)"`)
- [ ] `homyak/core/models.py` импортируется (`uv run python -c "from homyak.core.models import Base; print(sorted(Base.metadata.tables))"` → `['clusters', 'ingest_state', 'news_items']`)
- [ ] `alembic.ini` + `alembic/env.py` настроены
- [ ] `alembic/versions/0001_initial.py` создан
- [ ] `uv run alembic upgrade head` проходит без ошибок
- [ ] `psql ... -c '\dt'` показывает 4 таблицы: `alembic_version`, `clusters`, `ingest_state`, `news_items`
- [ ] `psql ... -c '\d news_items'` показывает индексы `idx_news_unprocessed`, `idx_news_published`, `idx_news_cluster`, `idx_news_fts` и UNIQUE `(source_type, source_id)`
- [ ] `uv run alembic downgrade base` проходит (обратимость миграции)
- [ ] Повторный `uv run alembic upgrade head` — без ошибок

---

## Acceptance criteria

```bash
cd /Users/maks/projects/homyak

# 1. Инфра
docker compose up -d postgres

# 2. Python env
uv sync

# 3. Конфиг
cp -n .env.example .env

# 4. Миграция
uv run alembic upgrade head

# 5. Проверка
psql "postgresql://homyak:homyak@localhost:5432/homyak" -c '\dt'
# Вывод содержит: alembic_version | clusters | ingest_state | news_items

# 6. Обратимость
uv run alembic downgrade base
uv run alembic upgrade head
```

Когда все пункты зелёные — Phase 1 завершена. Далее **Phase 2 — MVP feedthrough** (см. `architecture.md § Phasing`).

---

## Критические файлы для создания

| Путь | Назначение |
|---|---|
| `pyproject.toml` | uv project metadata + deps |
| `.python-version` | `3.13` |
| `.env.example` | Шаблон переменных |
| `.gitignore` | Стандартный Python + `.env` |
| `docker-compose.yml` | postgres + qdrant + nats |
| `alembic.ini` | Alembic config |
| `alembic/env.py` | Async engine для миграций |
| `alembic/versions/0001_initial.py` | Схема БД |
| `homyak/__init__.py` | Пакет-маркер |
| `homyak/core/__init__.py` | Пакет-маркер |
| `homyak/core/config.py` | pydantic-settings |
| `homyak/core/models.py` | SQLAlchemy declarative |
| `scripts/verify-phase-1.sh` | Acceptance-команда |
