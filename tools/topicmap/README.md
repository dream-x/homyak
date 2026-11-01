# Карта тем (офлайн-снимок)

Все обработанные записи ленты на одной плоскости: близкие по смыслу — рядом. Не сервис, а
снимок, который считается на Маке и публикуется как HTML-артефакт (на VM нет venv/ensurepip,
а UMAP + HDBSCAN за ~1 минуту считаются на ноутбуке).

Пайплайн (из этой папки):

1. Выборка — 20 000 случайных обработанных записей за 90 дней (id, заголовок, вертикаль,
   теги, скоринг, url, лента, дата):
   `ssh vm 'cd ~/homyak && docker compose exec -T postgres psql -U homyak -d homyak -q -f /dev/stdin' < sample.sql > sample.csv`
   и фидбэк той же обёрткой:
   `ssh vm 'cd ~/homyak && docker compose exec -T postgres psql -U homyak -d homyak -q -c "copy (select news_item_id, signal from feedback) to stdout csv"' > feedback.csv`.
2. Векторы bge-m3 — из Qdrant VM по id (Qdrant открыт на 6333):
   `uv run --with numpy --with qdrant-client python fetch_vectors.py` → `vectors.npy`, `ids.npy`.
3. Проекция и темы: `uv run --python 3.13 --with umap-learn --with hdbscan --with numpy python umap_run.py 30 5 leaf`
   → `topicmap.json` (UMAP cosine, n_neighbors 15, min_dist 0.1; HDBSCAN min_cluster_size 30,
   min_samples 5, leaf — даёт ~150 тем вместо одного гигантского IT-кластера при eom).
   Проекция кэшируется в `xy.npy` с привязкой к `ids.npy` (`xy_ids.npy`): новая выборка —
   новый UMAP, старый кэш не подхватится.
   Подписи тем — теги с наибольшим весом (частота в кластере × log(N/df), tf·idf), слова заголовков
   только как запасной вариант; широкие теги (`BROAD`) исключены.
   Перед публикацией вычистить U+FFFD из json (артефакт их не принимает).
4. Страница `topicmap.html` + `topicmap.json` рядом → Artifact (`files: {"topicmap.json": …}`).
   Цвета серий — валидированная палитра dataviz (3 слота, обе темы); золотое кольцо — ⭐/👍.
