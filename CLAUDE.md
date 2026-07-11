# Homyak — гайд для Claude Code

Персональный агрегатор новостей под интересы пользователя. Плагинная (sources/analyzers/outputs) +
event-driven (NATS JetStream) архитектура. Флагманская фича — гибридный персональный ранкер (Phase 6).

## Документация — источник истины
- `docs/architecture.md` — архитектура, схема БД, pipeline, § Key decisions, § Failure modes.
- `docs/phase-1-skeleton.md` … `docs/phase-6-personalization.md` — планы фаз с Acceptance/Checklist.
- **Всегда сверяйся с phase-доком** перед реализацией и гони его Acceptance дословно.

## Стек и конвенции
- Python **3.13**, менеджер — **uv** (`uv sync`, `uv run ...`, `uv add ...`). Не pip/poetry. `uv.lock` в git.
- FastAPI, SQLAlchemy 2.x **async** (asyncpg), Alembic (ручные обратимые миграции), pydantic v2 +
  pydantic-settings, structlog. Postgres 17, Qdrant, NATS JetStream, Ollama (bge-m3 + Qwen 2.5-14B).
- Async везде на I/O. Sync-библиотеки (feedparser) — через `asyncio.to_thread`.
- Секреты только из `.env` (env-vars), никогда в `config/sources.yaml` (там `token_env: NAME`).
- Чистую логику (нормализация URL, свёртка score, EMA) держи отдельными функциями и покрывай тестами.

## Инварианты (не нарушать)
- Source не пишет в БД — только выдаёт `NewsItemDTO`. Analyzer мутирует `AnalyzerContext`, не возвращает.
- Плагины — через `core/registry.py` / `config/sources.yaml`, не хардкод в `pipeline/*`.
- Ingest идемпотентен: `(source_type, source_id)` UNIQUE, `ON CONFLICT DO UPDATE`, publish при `was_new`.
- NATS subjects: `homyak.items.ingested|processed|output`, `homyak.feedback.recorded`. Consumer'ы с
  ack/nak+backoff, `max_deliver`, `ack_wait`. Sweeper переопубликует зависшее.
- Миграции обратимы (`upgrade→downgrade→upgrade`), циклические FK — отдельным шагом.

## Скиллы и агенты проекта (`.claude/`)
- Skill `homyak-adapter` — добавление source/analyzer/output адаптера.
- Skill `homyak-migration` — написание/проверка Alembic-миграции.
- Skill `homyak-run` — поднять инфру, запустить процессы, прогнать acceptance (+ fallback без docker).
- Agent `homyak-reviewer` — архитектурный ревью диффа перед коммитом.

## Окружение (на текущей машине)
- `uv`, `python3.13`, `psql` (client 14) — есть.
- **Docker нет, но есть Podman** (5.7.1, machine running; `podman compose` = Docker Compose v2). Qdrant поднят
  через `podman compose up -d qdrant`.
- **Postgres** — локальный `postgresql@14` через brew (роль/база `homyak` + `homyak_test`).
- **NATS** — нативный `nats-server` (brew 2.14.3): `nats-server -js -sd <dir> -m 8222`.
- **Ollama** — нативно (0.31.2), модели `bge-m3` (1024-dim) и `qwen2.5:14b` скачаны.
- Порядок запуска инфры для разработки — см. skill `homyak-run` / `scripts/verify-phase-*.sh`.

## Порядок работ
Идём по фазам снизу вверх: 1 (скелет) → 2 (feedthrough) → 3 (embeddings+TG+SSE) → 4 (LLM+бот+CLI) →
6 (персонализация, флагман; зависит от 3 и 4, но не от 5). Phase 5 (Twitter+WebUI) — опционально позже.

## Git
- Коммить по завершении осмысленного куска. Сообщения на русском, префикс типа (`feat:`/`docs:`/`fix:`).
- Не коммить `.env`, `.venv/`, кэши (см. `.gitignore`).
