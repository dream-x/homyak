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

## Окружение и запуск
**ПРОД — VM `homyak` (Proxmox, 192.168.140.20, `ssh maks@192.168.140.20`, ключ `~/.ssh/homelab`)**,
работает 24/7 (Mac ночью спит — поэтому переехали). Там docker compose в `~/homyak`: весь стек
(postgres/qdrant/nats + migrate + 7 сервисов) + gost-прокси (`TELEGRAM_BOT_PROXY` для Bot API) +
tscrapper-контейнер (`~/tscrapper`, MTProto через `TG_PROXY`) + мониторинг + RSSHub.
- **Деплой на VM**: у VM нет кредов к GitHub → `git push` с Мака, затем на VM `git pull` не работает;
  проверенный путь — `git bundle create b.bundle <vm-head>..master` → scp → на VM `git pull /tmp/b.bundle
  master` → `docker compose up -d --build <сервис>`.
- **Telegram-сессия tscrapper живёт ТОЛЬКО на VM.** Запуск копии с Мака = Telegram отзывает ключ
  («used under two different IP addresses», уже дважды). Релогин — `login.py` на VM (см. его докстринг).
- LLM: основной — 5090-бокс `white` (192.168.100.235:11434, qwen3.6:27b), фолбэк на Mac Metal
  (qwen3.5:9b) — только у локального стека, через `.env`.

**Локально (Mac) — только dev**: тот же compose через **Podman** (`podman compose up -d / down`).
⚠️ Локальный tgbot конфликтует с VM-ботом за getUpdates (один токен!) — не поднимай полный стек,
только нужные сервисы (`podman compose up -d api`). Тесты: `uv run pytest` с хоста против
контейнерного postgres (нужна БД `homyak_test`).

## Состояние (2026-07-20)
Реализованы Phase 1-4 + Phase 6.0-6.3 (флагман закрыт). Пайплайн (8 стадий): url_dedup → embedder →
similarity_dedup → llm_tagger → llm_summarizer → scorer → llm_relevance (судья) → personalizer. Процессы:
`homyak-ingest-poll` (RSS), `homyak-telegram-ingest` (TG из tscrapper через NATS `homyak.telegram.raw`),
`homyak-processor`, `homyak-learner` (обучение на 👍/👎), `homyak-sweeper`, `homyak-tgbot`, `homyak-api`.
CLI: `homyak-cli`, `homyak-interests` (show/diff/apply/backfill), `homyak-reembed`.
Сверх фаз: дашборд `/dashboard` + лента `/lenta` (👍/👎, Разбор, сортировка свежесть/скоринг),
публикация ленты в TG-канал (`CHANNEL_VERTICALS`), `/ask` RAG-дайджест, HN/Lobsters-источники.
**Интересы — только `config/interests.yaml`** (verticals + watch + weights), применять `homyak-interests
apply`. Выученное (affinity/taste/muted_tags) в БД и в файл не пишет.
Токен бота — в `.env` (gitignored). Telegram-каналы задаются в config.yaml tscrapper'а.

## Порядок работ
Фазы снизу вверх: 1 (скелет) → 2 (feedthrough) → 3 (embeddings+TG+SSE) → 4 (LLM+бот+CLI) →
6 (персонализация, флагман; зависит от 3 и 4, но не от 5). Phase 5 (Twitter+WebUI) — опционально позже.

## Git
- Коммить по завершении осмысленного куска. Сообщения на русском, префикс типа (`feat:`/`docs:`/`fix:`).
- Не коммить `.env`, `.venv/`, кэши (см. `.gitignore`).
