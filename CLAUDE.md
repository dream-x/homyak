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

## Окружение и запуск (Podman)
Весь стек — в **Podman** (`podman compose`, machine running). Одна команда:
```
podman compose up -d      # postgres, qdrant, nats + migrate + 7 app-сервисов
podman compose ps         # статус ; podman logs homyak-<service>-1 — логи
podman compose down       # стоп (volumes сохраняются)
```
- **Ollama — на хосте** (нативно, Metal): контейнеры смотрят на `host.containers.internal:11434`.
  Модели `bge-m3` (эмбеддинги), `qwen2.5:14b` (судья/теги), `qwen3:32b` (саммари).
- Postgres/Qdrant/NATS — контейнеры (volumes `homyak_pgdata`/`homyak_qdrant_storage`/`homyak_nats_data`).
  Postgres на host:5432 (пароль homyak), `homyak` + `homyak_test`. Образ — один на все сервисы (Dockerfile).
- **tscrapper** — на хосте (свой Telegram-session), публикует в NATS `homyak.telegram.raw` (порт 4222 проброшен).
- Тесты: `uv run pytest` с хоста против контейнерного postgres (нужна БД `homyak_test`).

## Состояние (2026-07-12)
Реализованы Phase 1-4 + Phase 6.0/6.1/6.2. Пайплайн (8 стадий): url_dedup → embedder → similarity_dedup
→ llm_tagger → llm_summarizer → scorer → llm_relevance (судья) → personalizer. Процессы:
`homyak-ingest-poll` (RSS), `homyak-telegram-ingest` (TG из tscrapper через NATS `homyak.telegram.raw`),
`homyak-processor`, `homyak-learner` (обучение на 👍/👎), `homyak-sweeper`, `homyak-tgbot`, `homyak-api`.
CLI: `homyak-cli`, `homyak-interests` (show/diff/apply/backfill), `homyak-reembed`.
**Интересы — только `config/interests.yaml`** (verticals + watch + weights), применять `homyak-interests
apply`. Выученное (affinity/taste/muted_tags) в БД и в файл не пишет. Осталось Phase 6.3.
Токен бота — в `.env` (gitignored). Telegram-каналы задаются в config.yaml tscrapper'а.

## Порядок работ
Фазы снизу вверх: 1 (скелет) → 2 (feedthrough) → 3 (embeddings+TG+SSE) → 4 (LLM+бот+CLI) →
6 (персонализация, флагман; зависит от 3 и 4, но не от 5). Phase 5 (Twitter+WebUI) — опционально позже.

## Git
- Коммить по завершении осмысленного куска. Сообщения на русском, префикс типа (`feat:`/`docs:`/`fix:`).
- Не коммить `.env`, `.venv/`, кэши (см. `.gitignore`).
