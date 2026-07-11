#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Инфра Phase 3: Postgres + NATS + Qdrant + Ollama.
#   Qdrant: podman/docker compose up -d qdrant   (или бинарь)
#   Ollama: нативно (Mac app / brew) или docker; модели:
#     ollama pull bge-m3
#     ollama pull qwen2.5:14b        # нужен с Phase 4
#   NATS:   nats-server -js -sd <dir> -m 8222

uv sync
uv run alembic upgrade head          # 0001..0003

echo "--- bge-m3 отдаёт 1024-dim? ---"
curl -sf "${OLLAMA_URL:-http://localhost:11434}/api/embed" \
  -d '{"model":"bge-m3","input":"hi"}' \
  | python3 -c "import sys,json; e=json.load(sys.stdin)['embeddings'][0]; print('dim', len(e))"

echo "--- Qdrant жив? ---"
curl -sf "${QDRANT_URL:-http://localhost:6333}/healthz" && echo " qdrant ok"

# Процессы: uv run homyak-processor  (теперь embedder+similarity_dedup)
#           uv run homyak-ingest-poll ; uv run homyak-api
# Telegram: uv run homyak-tg-relay   (после патча tscrapper + TELEGRAM_OUTBOX_PATH)

PSQL_URL="postgresql://homyak:homyak@localhost:5432/homyak"
echo "--- эмбеддинги проставлены ---"
psql "$PSQL_URL" -c "SELECT count(*) FILTER (WHERE embedding_version IS NOT NULL) AS embedded, count(*) FROM news_items"
echo "--- Qdrant points ---"
curl -sf "${QDRANT_URL:-http://localhost:6333}/collections/news_items" \
  | python3 -c "import sys,json; print('points:', json.load(sys.stdin)['result']['points_count'])"
echo "--- similarity: кластеры с несколькими источниками ---"
psql "$PSQL_URL" -c "SELECT cluster_id, array_agg(DISTINCT source_type) srcs, count(*) n FROM news_items WHERE cluster_id IS NOT NULL GROUP BY cluster_id HAVING count(*)>1 LIMIT 5"

echo "--- SSE (5с) ---"
curl -sN -m5 "localhost:8000/feed/stream" | head -5 || true

echo "OK: Phase 3 verified"
