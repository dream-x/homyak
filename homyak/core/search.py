"""Гибридный поиск по накопленной ленте: лексика (Postgres FTS) + семантика (Qdrant), слитые RRF,
с бустами намерений (свежесть/проекты) и опциональным LLM-переранжированием.

Почему так: чистый websearch_to_tsquery ANDит все слова — русский NL-запрос («свежие ии чат проекты
для селф хостинга») не матчит ничего, и гибрид вырождается в одну семантику, которая на многосоставном
запросе расплывается. Поэтому: лексика через OR-tsquery (частичные совпадения ранжируются), намерения
(«свежие» → буст свежести, «проект/селфхост» → буст gh_-репозиториев) добавляются как ДОП. RRF-списки,
а LLM отбирает финал. Чистые хелперы (_lexical_or, _intent, RRF) — тестируются без БД.
"""

from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import bindparam, text

from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.core.config import settings
from homyak.core.llm import OllamaLLM
from homyak.storage.db import SessionFactory
from homyak.storage.qdrant import QdrantStore

RRF_K = 60
POOL = 80

_WORD = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ]{2,}")
# стоп-слова (ru+en) + слова-намерения свежести: в лексику их не берём (шум/обрабатываются бустом)
_STOP = {
    "для", "и", "в", "во", "на", "с", "со", "по", "из", "от", "к", "о", "об", "как", "что",
    "это", "the", "a", "an", "for", "of", "to", "and", "or", "in", "on", "with", "how", "what",
}
_FRESH = {"свежие", "свежий", "свежее", "новые", "новый", "последние", "последний", "fresh",
          "latest", "recent", "new", "newest"}
_PROJECT = {"проект", "проекты", "репозитори", "репозиторий", "репа", "репы", "github", "гитхаб",
            "selfhost", "self", "селфхост", "селф", "хостинг", "хостинга", "selfhosted", "opensource",
            "опенсорс", "tool", "tools", "инструмент", "инструменты", "lib", "library", "библиотека"}


def reciprocal_rank_fusion(ranked_lists: list[list[int]], k: int = RRF_K) -> list[int]:
    """RRF: id → сумма 1/(k + rank) по всем спискам, где встретился. По убыванию, стабильно на ничьих."""
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


def _tokens(query: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(query or "")]


def _lexical_or(query: str) -> str | None:
    """OR-tsquery из значимых слов запроса (без стоп-слов и слов-свежести). None, если пусто.

    Только alnum-токены → безопасно для to_tsquery без экранирования спецсимволов.
    """
    words = [w for w in _tokens(query) if w not in _STOP and w not in _FRESH and len(w) >= 2]
    seen: list[str] = []
    for w in words:  # уникальные, порядок сохраняем
        if w not in seen:
            seen.append(w)
    return " | ".join(seen) if seen else None


def _intent(query: str) -> dict:
    """Намерения запроса: freshness (сортировать по свежести) и project (бустить gh_-репозитории)."""
    toks = set(_tokens(query))
    return {
        "fresh": bool(toks & _FRESH),
        "project": bool(toks & _PROJECT),
    }


def _kind_cond(kind: str) -> tuple[str, dict]:
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
    """Вопрос → ранжированный список записей (лексика OR + семантика, RRF + бусты, схлоп кластеров)."""
    q = (query or "").strip()
    if not q:
        return []
    intent = _intent(q)

    # --- семантика (Qdrant) ---
    qdrant = QdrantStore(settings.qdrant_url)
    try:
        vec = await EmbedderAnalyzer(qdrant)._embed(q)
        sem = await qdrant.search_similar(vec, limit=POOL, score_threshold=0.30)
    finally:
        await qdrant.close()
    sem_ids = [h[0] for h in sem]

    # --- лексика (Postgres FTS, OR) ---
    lex_ids: list[int] = []
    or_query = _lexical_or(q)
    if or_query:
        async with SessionFactory() as s:
            lex_ids = [
                r.id
                for r in (
                    await s.execute(
                        text(
                            "select id from news_items"
                            " where processed_at is not null and skip_reason is null"
                            " and search_tsv @@ to_tsquery('simple', :q)"
                            " order by ts_rank_cd(search_tsv, to_tsquery('simple', :q)) desc"
                            " limit :pool"
                        ),
                        {"q": or_query, "pool": POOL},
                    )
                ).all()
            ]

    # кандидатский пул (объединение дорожек) — по нему тянем метаданные и строим бусты
    pool_ids: list[int] = []
    for i in sem_ids + lex_ids:
        if i not in pool_ids:
            pool_ids.append(i)
    pool_ids = pool_ids[: POOL * 2]
    if not pool_ids:
        return []

    kind_sql, params = _kind_cond(kind)
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
                    " coalesce(published_at,fetched_at) published, extract(epoch from (now()-coalesce(published_at,fetched_at)))::int age_s"
                    f" from news_items where {where} and id in :ids"
                ).bindparams(bindparam("ids", value=pool_ids, expanding=True)),
                params,
            )
        ).all()
    by_id = {r.id: r for r in rows}
    pool = [i for i in pool_ids if i in by_id]  # прошедшие фильтры, в порядке пула

    # --- RRF: семантика + лексика + бусты намерений (как доп. ранжированные списки) ---
    lists = [sem_ids, lex_ids]
    if intent["fresh"]:
        lists.append(sorted(pool, key=lambda i: by_id[i].age_s))  # свежайшие первыми
    if intent["project"]:
        gh = [i for i in pool if (by_id[i].feed_name or "").startswith("gh_")
              or "github.com" in (by_id[i].url or "")]
        if gh:
            lists.append(gh)
    fused = reciprocal_rank_fusion(lists)

    # --- сборка: порядок RRF, схлоп кластеров ---
    seen_clusters: set[int] = set()
    out: list[dict] = []
    for iid in fused:
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
                    "twitter" if (r.feed_name or "").startswith("tw_") else (r.source_type or "other")
                ),
                "feed": r.feed_name,
                "vertical": r.vertical,
                "score": r.personal_score,
                "summary": (r.summary or "")[:200] or None,
                "url": r.url,
                "tags": list(r.tags or [])[:4],
                "published": r.published,
                "age_s": r.age_s,
            }
        )
        if len(out) >= limit:
            break
    return out


_RERANK_SYS = (
    "You re-rank search results for a user's query. Respect EVERY constraint in the query — topic, "
    "type (e.g. project/repository vs news), and recency (e.g. 'fresh'/'свежие'). Given the query and a "
    "numbered list of candidates, return STRICT JSON {\"order\":[indices]} listing ONLY the indices that "
    "genuinely match, best first; DROP the ones that don't fit. Language-agnostic. Indices are 0-based."
)


async def rerank(query: str, items: list[dict], llm: OllamaLLM | None = None, *, pool: int = 30) -> list[dict]:
    """LLM отбирает/сортирует кандидатов под точный смысл запроса. Ошибка LLM → исходный порядок."""
    if len(items) <= 1:
        return items
    head = items[:pool]
    lines = []
    for i, it in enumerate(head):
        age_h = round((it.get("age_s") or 0) / 3600)
        lines.append(f"{i}. {it.get('title') or '—'} · {it.get('feed') or ''} · {age_h}h")
    user = f"Query: {query}\n\nCandidates:\n" + "\n".join(lines)
    try:
        data = await (llm or OllamaLLM()).chat_json(_RERANK_SYS, user)
        order = data.get("order") if isinstance(data, dict) else None
        if not isinstance(order, list) or not order:
            return items
        picked = [head[i] for i in order if isinstance(i, int) and 0 <= i < len(head)]
        if not picked:
            return items
        # хвост (не попавший в pool) — следом, чтобы ничего молча не потерять
        chosen_ids = {it["id"] for it in picked}
        tail = [it for it in items if it["id"] not in chosen_ids]
        return picked + tail
    except Exception:
        return items
