"""GitHub-радар: какие репозитории из ленты набирают звёзды быстрее всех.

Лента приносит репозитории (trending/search/starred через RSSHub, ссылки в постах и README как
текст), но не говорит, растут ли они. Раз в сутки sweeper снимает stars/forks по репозиториям,
бывшим в ленте за месяц, и по всем ⭐-помеченным (`repo_stats`, ключ (repo, day)); радар считает
скорость — звёзд в день за окно — по первому и последнему снимку окна. Без двух снимков скорости
нет: в первый день после деплоя радар пуст, это нормально.

Чистые части (ключ репозитория, скорость) — без БД; снимок принимает fetch-функцию, чтобы тест
не ходил в GitHub.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Awaitable, Callable

import httpx
import structlog
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from homyak.core.config import settings
from homyak.core.github import repo_of
from homyak.core.models import RepoStats
from homyak.storage import db

log = structlog.get_logger(__name__)

_API = "https://api.github.com/repos/{repo}"
# та же нормализация, что repo_key, но в SQL — чтобы связать repo_stats с записями ленты
REPO_KEY_SQL = (  # 'i' — как и в REPO_URL_SQL: GitHub.com/A/B — тот же репозиторий
    "lower(regexp_replace(regexp_replace(url,"
    " '^https?://(www\\.)?github\\.com/([^/#?]+)/([^/#?]+).*$', '\\2/\\3', 'i'), '\\.git$', ''))"
)
REPO_URL_SQL = "url ~* '^https?://(www\\.)?github\\.com/[^/#?]+/[^/#?]+'"
_MAX_ERROR_STREAK = 5  # столько сетевых ошибок подряд = сеть легла, дальше только шум


class RateLimited(RuntimeError):
    "GitHub сказал «хватит»: в этом прогоне дальше смысла нет, остальное — завтра."


def is_rate_limit(status: int, headers) -> bool:
    "403 у GitHub — не только лимит (TOS-блок репозитория, SAML, abuse): лимит узнаём по заголовкам."
    if status == 429:
        return True
    return status == 403 and (
        headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers
    )


def repo_key(url: str | None) -> str | None:
    """github.com/Owner/Repo/… → «owner/repo» (lower-case), иначе None."""
    pair = repo_of(url)
    return f"{pair[0]}/{pair[1]}".lower() if pair else None


def velocity(points: list[tuple[date, int]]) -> dict | None:
    """Скорость по первому и последнему снимку: (stars, gained, per_day, span_days). None — мало."""
    pts = sorted(points)
    if len(pts) < 2:
        return None
    (d0, s0), (d1, s1) = pts[0], pts[-1]
    span = max(1, (d1 - d0).days)
    gained = s1 - s0
    return {"stars": s1, "gained": gained, "per_day": round(gained / span, 1), "span_days": span}


async def candidate_repos(sf=None, days: int = 30, limit: int = 300) -> list[str]:
    """Репозитории для снимка: все ⭐-помеченные, затем те, у кого в окне уже есть снимок, затем
    свежие из ленты за `days` дней.

    Порядок важен, когда бюджет меньше числа кандидатов (без токена — 60 запросов/час): без
    второго приоритета свежие URL из gh_*-фидов каждый день вытесняли бы вчерашние, первые
    точки никогда не замыкались бы в пару, и радар вырождался бы в «только ⭐».
    """
    async with (sf or db.SessionFactory)() as s:
        rows = (
            await s.execute(
                text(
                    "select url, bool_or(starred) starred, max(fetched_at) last_seen"
                    " from (select n.url, n.fetched_at,"
                    "   exists (select 1 from feedback f where f.news_item_id = n.id"
                    "           and f.signal = 'save') starred"
                    f"   from news_items n where {REPO_URL_SQL}) x"
                    " group by url"
                    " having bool_or(starred) or max(fetched_at) > now() - make_interval(days => :d)"
                    " order by bool_or(starred) desc, max(fetched_at) desc"
                ),
                {"d": days},
            )
        ).all()
        snapped = {
            r[0]
            for r in await s.execute(
                text("select distinct repo from repo_stats where day >= current_date - cast(:d as integer)"),
                {"d": days},
            )
        }
    agg: dict[str, list] = {}  # key → [starred, has_snapshot, last_seen]
    for r in rows:
        key = repo_key(r.url)
        if not key:
            continue
        a = agg.setdefault(key, [False, key in snapped, None])
        a[0] = a[0] or bool(r.starred)
        if r.last_seen and (a[2] is None or r.last_seen > a[2]):
            a[2] = r.last_seen
    ordered = sorted(
        agg,
        key=lambda k: (not agg[k][0], not agg[k][1], -(agg[k][2].timestamp() if agg[k][2] else 0)),
    )
    return ordered[:limit]


async def fetch_repo(repo: str, client: httpx.AsyncClient) -> dict | None:
    """GET /repos/{repo} → {stars, forks, open_issues, pushed_at}; None — нет/переименован/лимит."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "homyak"}
    if settings.github_access_token:
        headers["Authorization"] = f"Bearer {settings.github_access_token}"
    resp = await client.get(_API.format(repo=repo), headers=headers)
    if is_rate_limit(resp.status_code, resp.headers):
        raise RateLimited(f"github rate limit: {resp.status_code}")
    if resp.status_code != 200:  # 404 — нет/переименован, 403 без лимита — заблокирован
        return None
    d = resp.json()
    pushed = d.get("pushed_at")
    return {
        "stars": int(d.get("stargazers_count") or 0),
        "forks": int(d.get("forks_count") or 0),
        "open_issues": int(d.get("open_issues_count") or 0),
        "pushed_at": datetime.fromisoformat(pushed.replace("Z", "+00:00")) if pushed else None,
    }


Fetcher = Callable[[str], Awaitable[dict | None]]


async def snapshot(
    sf=None, fetch: Fetcher | None = None, limit: int | None = None, today: date | None = None
) -> dict:
    """Дневной снимок всех кандидатов. Возвращает {repos, saved, missing, errors, stopped}.

    Останавливается только по лимиту GitHub (RateLimited) или после _MAX_ERROR_STREAK сетевых
    ошибок подряд; таймаут одного репозитория — его проблема, а не всего снимка.
    """
    limit = limit if limit is not None else settings.repo_stats_limit
    repos = await candidate_repos(sf, limit=limit)
    day = today or date.today()
    saved = missing = errors = streak = 0
    stopped: str | None = None
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        _fetch = fetch or (lambda r: fetch_repo(r, client))
        for i, repo in enumerate(repos):
            try:
                stats = await _fetch(repo)
            except RateLimited as e:  # дальше смысла нет, остальное — завтра
                stopped = str(e)[:120]
                log.warning("repo_stats_stopped", at=i, repo=repo, error=stopped)
                break
            except Exception as e:  # сеть/JSON одного репозитория — пропускаем
                errors += 1
                streak += 1
                log.warning("repo_stats_fetch_failed", repo=repo, error=str(e)[:120])
                if streak >= _MAX_ERROR_STREAK:
                    stopped = f"{streak} ошибок подряд: {str(e)[:80]}"
                    log.warning("repo_stats_stopped", at=i, repo=repo, error=stopped)
                    break
                continue
            streak = 0
            if stats is None:
                missing += 1
                continue
            async with (sf or db.SessionFactory)() as s:
                stmt = pg_insert(RepoStats).values(repo=repo, day=day, **stats)
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["repo", "day"],
                        set_={k: getattr(stmt.excluded, k) for k in stats} | {"fetched_at": func.now()},
                    )
                )
                await s.commit()
            saved += 1
            if fetch is None and i % 20 == 19:
                await asyncio.sleep(0.5)  # вежливость к API
    return {
        "repos": len(repos), "saved": saved, "missing": missing, "errors": errors, "stopped": stopped,
    }


async def radar(days: int = 7, limit: int = 20, sf=None) -> list[dict]:
    """Топ по звёздам/день за окно + запись ленты, которая репозиторий принесла, + ⭐."""
    days = max(2, min(int(days or 7), 90))
    async with (sf or db.SessionFactory)() as s:
        rows = (
            await s.execute(
                text(
                    "with w as (select repo, day, stars, forks, pushed_at from repo_stats"
                    "   where day >= current_date - cast(:d as integer)),"
                    " f as (select distinct on (repo) repo, day d0, stars s0 from w order by repo, day),"
                    " l as (select distinct on (repo) repo, day d1, stars s1, forks, pushed_at"
                    "   from w order by repo, day desc)"
                    " select f.repo, f.d0, l.d1, f.s0, l.s1, l.forks, l.pushed_at,"
                    "   l.s1 - f.s0 gained, (l.s1 - f.s0)::float / greatest(1, l.d1 - f.d0) per_day"
                    " from f join l using (repo) where l.d1 > f.d0"
                    " order by per_day desc, gained desc limit :lim"
                ),
                {"d": days, "lim": limit},
            )
        ).all()
        keys = [r.repo for r in rows]
        items = {}
        if keys:
            for it in (
                await s.execute(
                    text(
                        f"select distinct on (key) key, id, title, url, feed_name, personal_score,"
                        f" starred from (select {REPO_KEY_SQL} key, id, title, url, feed_name,"
                        "   personal_score, fetched_at,"
                        "   exists (select 1 from feedback f where f.news_item_id = news_items.id"
                        "           and f.signal = 'save') starred"
                        f"   from news_items where {REPO_URL_SQL}) x"
                        " where key = any(cast(:keys as text[]))"
                        " order by key, starred desc, personal_score desc nulls last, fetched_at desc"
                    ),
                    {"keys": keys},
                )
            ).all():
                items[it.key] = it
    out = []
    for r in rows:
        it = items.get(r.repo)
        out.append(
            {
                "repo": r.repo,
                "stars": int(r.s1),
                "gained": int(r.gained),
                "per_day": round(float(r.per_day), 1),
                "span_days": int((r.d1 - r.d0).days),
                "forks": int(r.forks),
                "pushed_at": r.pushed_at.isoformat() if r.pushed_at else None,
                "url": f"https://github.com/{r.repo}",
                "item": (
                    {
                        "id": it.id, "title": it.title, "url": it.url, "feed": it.feed_name,
                        "score": it.personal_score,
                    }
                    if it is not None
                    else None
                ),
                "starred": bool(it.starred) if it is not None else False,
            }
        )
    return out
