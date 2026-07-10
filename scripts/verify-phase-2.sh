#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Инфра: Postgres + NATS JetStream. Docker если есть, иначе — локальные бинари (см. skill homyak-run).
# Postgres: docker compose up -d postgres  |  или brew postgresql@14
# NATS:     docker compose up -d nats       |  или nats-server -js -sd <dir> -m 8222

cp -n .env.example .env || true
uv sync
uv run alembic upgrade head        # 0001 + 0002

PSQL_URL="postgresql://homyak:homyak@localhost:5432/homyak"

# 3 процесса (в отдельных терминалах или фоном):
#   uv run homyak-processor
#   uv run homyak-ingest-poll
#   uv run homyak-api
# затем подождать 1-2 цикла (или POST /admin/sources/rss:hn/repoll)

echo "--- источники ---"
psql "$PSQL_URL" -c "SELECT source_type, count(*) FROM news_items GROUP BY 1 ORDER BY 2 DESC"
echo "--- кластеры size>1 (URL-дедуп) ---"
psql "$PSQL_URL" -c "SELECT count(*) FROM (SELECT cluster_id FROM news_items WHERE cluster_id IS NOT NULL GROUP BY cluster_id HAVING count(*)>1) t"
echo "--- API ---"
curl -sf "localhost:8000/healthz"        && echo " healthz ok"
curl -sf "localhost:8000/feed?limit=20"  | python3 -c "import sys,json; print('feed items:', len(json.load(sys.stdin)['items']))"
curl -sf "localhost:8000/feed.rss"       | xmllint --noout - && echo "rss valid"
curl -sf "localhost:8000/feed.json"      | python3 -c "import sys,json; print('json feed:', json.load(sys.stdin)['version'])"

echo "OK: Phase 2 verified"
