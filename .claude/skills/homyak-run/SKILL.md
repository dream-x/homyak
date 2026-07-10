---
name: homyak-run
description: Поднять инфраструктуру Homyak, запустить процессы и прогнать acceptance-проверки фазы. Используй, когда надо запустить/проверить приложение, поднять Postgres/NATS/Qdrant/Ollama, применить миграции или убедиться что фаза работает.
---

# Homyak: запуск и верификация

## Инфраструктура

Штатно — через docker compose (`postgres`, `qdrant`, `nats`, `ollama`). **Если Docker не установлен**
(проверь `docker ps`), используй fallback на локальные сервисы.

### Полный путь (docker есть)
```bash
cd /Users/maks/projects/homyak
docker compose up -d postgres          # Phase 1: только PG
# Phase 2+: docker compose up -d postgres nats
# Phase 3+: + qdrant ollama
until docker compose exec -T postgres pg_isready -U homyak >/dev/null 2>&1; do sleep 0.5; done
```

### Fallback без docker (локальный Postgres через Homebrew)
```bash
# проверить наличие сервера
brew services list | grep postgres || ls /opt/homebrew/opt/postgresql@*/bin/postgres 2>/dev/null
brew services start postgresql@17 2>/dev/null || brew services start postgresql@14
# создать роль/базу под .env (homyak:homyak)
psql postgres -c "CREATE ROLE homyak LOGIN PASSWORD 'homyak' CREATEDB;" 2>/dev/null || true
psql postgres -c "CREATE DATABASE homyak OWNER homyak;" 2>/dev/null || true
```
NATS/Qdrant/Ollama локально без docker: `brew install nats-server`, `ollama` (native app), Qdrant —
только через docker/бинарь. Для фаз, которым они нужны, docker де-факто обязателен — предупреди пользователя.

## Env + миграции
```bash
cp -n .env.example .env
uv sync
uv run alembic upgrade head
```

## Проверка без живой БД (когда инфра недоступна)
```bash
uv sync
uv run python -c "from homyak.core.models import Base; print(sorted(Base.metadata.tables))"
uv run python -c "from homyak.core.config import settings; print(settings.database_url)"
uv run alembic upgrade head --sql | head -60      # SQL-дифф миграции без применения
uv run pytest -q                                   # юнит-тесты, не требующие БД
```

## Запуск процессов (по фазам)
```bash
# Phase 2
uv run homyak-ingest-poll &
uv run homyak-processor &
uv run homyak-sweeper &
uv run homyak-api
# Phase 3+: uv run homyak-tg-relay &
# Phase 6:  uv run homyak-learner & ; uv run homyak-tgbot
```

## Acceptance
Всегда сверяйся с `docs/phase-N-*.md` § «Acceptance criteria» и § «Checklist» — там точные команды и
ожидаемый вывод для конкретной фазы. Гони их дословно и отмечай пункты.

## Правила
- Не оставляй фоновые процессы висеть после проверки — глуши их.
- Если Docker не запущен — не пытайся молча; сообщи пользователю (нужно поставить Docker Desktop/colima).
- Порог «фаза готова» = все пункты Checklist из phase-дока зелёные, включая обратимость миграции.
