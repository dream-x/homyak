#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Инфра: docker если есть, иначе — локальный Postgres (см. skill homyak-run).
if docker ps >/dev/null 2>&1; then
    docker compose up -d postgres
    until docker compose exec -T postgres pg_isready -U homyak >/dev/null 2>&1; do sleep 0.5; done
else
    echo "docker недоступен — ожидаю локальный Postgres на localhost:5432 (роль/база homyak)"
fi

cp -n .env.example .env || true
uv sync
uv run alembic upgrade head

PSQL_URL="postgresql://homyak:homyak@localhost:5432/homyak"
psql "$PSQL_URL" -c '\dt'
# Ожидаем: alembic_version  clusters  ingest_state  news_items
psql "$PSQL_URL" -c '\d news_items' | head -40

# Обратимость
uv run alembic downgrade base
uv run alembic upgrade head
echo "OK: Phase 1 verified"
