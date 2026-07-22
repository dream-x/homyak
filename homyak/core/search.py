"""Гибридный поиск по накопленной ленте: лексика (Postgres FTS) + семантика (Qdrant), слитые RRF.

Лексика ловит точные термины/имена/аббревиатуры (search_tsv — persisted tsvector, GIN-индекс),
семантика — по смыслу (bge-m3 в Qdrant). Reciprocal Rank Fusion объединяет два ранга: вверху то,
что всплыло в обоих. Чистую свёртку рангов держим отдельной функцией — тестируется без БД/Qdrant.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import bindparam, text

from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.core.config import settings
from homyak.storage.db import SessionFactory
from homyak.storage.qdrant import QdrantStore

RRF_K = 60  # стандартная константа RRF: гасит вклад хвоста, не даёт одному рангу доминировать
POOL = 80   # сколько кандидатов берём из каждой дорожки до слияния


def reciprocal_rank_fusion(ranked_lists: list[list[int]], k: int = RRF_K) -> list[int]:
    """RRF: id → сумма 1/(k + rank) по всем спискам, где он встретился. Возвращает id по убыванию.

    Порядок стабилен: при равных очках сохраняется относительный порядок первого появления.
    """
    score: dict[int, float] = defaultdict(float)
    first_seen: dict[int, int] = {}
    seq = 0
    for lst in ranked_lists:
        for rank, item_id in enumerate(lst):
            score[item_id] += 1.0 / (k + rank)
            if item_id not in first_seen:
                first_seen[item_id] = seq
                seq += 1
    return sorted(score, key=lambda i: (-score[i], first_seen[i]))


def _kind_cond(kind: str) -> tuple[str, dict]:
    """Фильтр вертикали/типа для итоговой выборки (как в ленте)."""
    if kind == "twitter":
        return "feed_name like 'tw_%'", {}
    if kind in ("business", "it", "medical"):
        return "vertical = :v", {"v": kind}
    if kind == "watch":
        return "cardinality(watch_topics) > 0", {}
    return "true", {}


async def hybrid_search(
    query: str,
    *,
    kind: str = "all",
    hours: int = 0,
    saved: bool = False,
    limit: int = 40,
) -> list[dict]:
    """Вопрос → ранжированный список записей (гибрид FTS+вектор, RRF, схлоп кластеров).

    kind: all|twitter|business|it|medical|watch. hours>0 — только за N часов.
    saved=True — только сохранённое/лайкнутое (feedback signal in up|save).
    """
    q = (query or "").strip()
    if not q:
        return []

    # --- семантика (Qdrant) ---
    qdrant = QdrantStore(settings.qdrant_url)
    try:
        vec = await EmbedderAnalyzer(qdrant)._embed(q)
        sem = await qdrant.search_similar(vec, limit=POOL, score_threshold=0.30)
    finally:
        await qdrant.close()
    sem_ids = [h[0] for h in sem]

    # --- лексика (Postgres FTS) ---
    async with SessionFactory() as s:
        lex_rows = (
            await s.execute(
                text(
                    "select id from news_items"
                    " where processed_at is not null and skip_reason is null"
                    " and search_tsv @@ websearch_to_tsquery('simple', :q)"
                    " order by ts_rank_cd(search_tsv, websearch_to_tsquery('simple', :q)) desc"
                    " limit :pool"
                ),
                {"q": q, "pool": POOL},
            )
        ).all()
    lex_ids = [r.id for r in lex_rows]

    fused = reciprocal_rank_fusion([sem_ids, lex_ids])
    if not fused:
        return []

    # --- добираем строки по слитому порядку + фильтры + схлоп кластеров ---
    kind_sql, params = _kind_cond(kind)
    params["ids"] = fused[: POOL * 2]
    conds = ["processed_at is not null", "skip_reason is null", kind_sql]
    if hours > 0:
        conds.append("coalesce(published_at, fetched_at) > now() - make_interval(hours => :hrs)")
        params["hrs"] = hours
    if saved:
        conds.append(
            "exists (select 1 from feedback f where f.news_item_id = news_items.id"
            " and f.signal in ('up','save'))"
        )
    where = " and ".join(conds)
    async with SessionFactory() as s:
        rows = (
            await s.execute(
                text(
                    "select id, title, source_type, feed_name, vertical, personal_score,"
                    " summary, url, cluster_id, tags,"
                    " extract(epoch from (now()-coalesce(published_at,fetched_at)))::int age_s"
                    f" from news_items where {where}"
                    " and id in :ids"
                ).bindparams(bindparam("ids", value=params.pop("ids"), expanding=True)),
                params,
            )
        ).all()

    by_id = {r.id: r for r in rows}
    seen_clusters: set[int] = set()
    out: list[dict] = []
    for iid in fused:  # сохраняем ранг RRF
        r = by_id.get(iid)
        if r is None:
            continue
        key = r.cluster_id or -r.id
        if key in seen_clusters:
            continue
        seen_clusters.add(key)
        out.append(
            {
                "id": r.id,
                "title": r.title,
                "bucket": (
                    "twitter"
                    if (r.feed_name or "").startswith("tw_")
                    else (r.source_type or "other")
                ),
                "feed": r.feed_name,
                "vertical": r.vertical,
                "score": r.personal_score,
                "summary": (r.summary or "")[:200] or None,
                "url": r.url,
                "tags": list(r.tags or [])[:4],
                "age_s": r.age_s,
            }
        )
        if len(out) >= limit:
            break
    return out
