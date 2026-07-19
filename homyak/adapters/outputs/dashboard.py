"""Realtime-дашборд пайплайна: SSE-поток событий NATS + снапшот-статистика Postgres + HTML.

Регистрируется напрямую на app в api.py. Показывает «что куда поступает»: сырой inflow по
источникам и обработанные items с маршрутом источник → вертикаль → score, в реальном времени.
"""

from __future__ import annotations

import json

import nats
import structlog
from fastapi import Request
from nats.js.api import ConsumerConfig, DeliverPolicy
from sqlalchemy import bindparam, text
from sse_starlette.sse import EventSourceResponse

from homyak.core.config import settings
from homyak.core.events import SUBJECT_INGESTED, SUBJECT_PROCESSED
from homyak.core.models import NewsItem
from homyak.storage.db import SessionFactory

log = structlog.get_logger(__name__)


def _bucket(source_type: str | None, feed_name: str | None) -> str:
    if feed_name and feed_name.startswith("tw_"):
        return "twitter"
    if source_type == "telegram":
        return "telegram"
    return source_type or "other"


async def stats_snapshot() -> dict:
    """Снимок агрегатов из Postgres для карточек и панелей дашборда."""
    async with SessionFactory() as s:
        totals = (
            await s.execute(
                text(
                    "select "
                    " count(*) filter (where processed_at is not null and vertical is not null) processed,"
                    " count(*) filter (where processed_at is null) pending,"
                    " count(*) filter (where pushed_at is not null) pushed,"
                    " count(*) filter (where processed_at > now() - interval '60 min') last_hour,"
                    " count(*) filter (where processed_at > now() - interval '10 min') last_10m,"
                    " count(*) filter (where skip_reason is not null) skipped"
                    " from news_items"
                )
            )
        ).first()
        by_vertical = (
            await s.execute(
                text(
                    "select vertical, count(*) from news_items"
                    " where vertical is not null group by vertical"
                )
            )
        ).all()
        by_source = (
            await s.execute(
                text(
                    "select case when feed_name like 'tw_%' then 'twitter'"
                    " when source_type='telegram' then 'telegram' else source_type end src,"
                    " count(*) from news_items"
                    " where processed_at is not null and vertical is not null group by 1 order by 2 desc"
                )
            )
        ).all()
        top_feeds = (
            await s.execute(
                text(
                    "select feed_name, count(*) from news_items"
                    " where processed_at > now() - interval '60 min' and feed_name is not null"
                    " group by feed_name order by 2 desc limit 10"
                )
            )
        ).all()
    return {
        "processed": totals.processed if totals else 0,
        "pending": totals.pending if totals else 0,
        "pushed": totals.pushed if totals else 0,
        "last_hour": totals.last_hour if totals else 0,
        "last_10m": totals.last_10m if totals else 0,
        "skipped": totals.skipped if totals else 0,
        "by_vertical": {v: n for v, n in by_vertical},
        "by_source": {v: n for v, n in by_source},
        "top_feeds": [{"feed": f, "n": n} for f, n in top_feeds],
    }


async def queue_snapshot(limit: int = 40) -> dict:
    """Очередь на обработку: pending-айтемы (processed_at IS NULL), старейшие сверху."""
    async with SessionFactory() as s:
        total = (
            await s.execute(text("select count(*) from news_items where processed_at is null"))
        ).scalar() or 0
        rows = (
            await s.execute(
                text(
                    "select id, title, source_type, feed_name,"
                    " extract(epoch from (now()-coalesce(published_at, fetched_at)))::int age_s,"
                    " extract(epoch from (now()-fetched_at))::int lag_s"
                    " from news_items where processed_at is null"
                    " order by fetched_at asc limit :lim"
                ),
                {"lim": limit},
            )
        ).all()
    return {
        "pending": total,
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "bucket": _bucket(r.source_type, r.feed_name),
                "feed": r.feed_name,
                "age_s": r.age_s,
                "lag_s": r.lag_s,
            }
            for r in rows
        ],
    }


async def watchlist_snapshot(per_topic: int = 6) -> dict:
    """Для каждой темы вотчлиста: счётчик + свежие айтемы (для панелей на дашборде)."""
    from homyak.core.interests import watch_topic_names

    names = watch_topic_names()
    async with SessionFactory() as s:
        counts = dict(
            (
                await s.execute(
                    text(
                        "select topic, count(*) from news_items, unnest(watch_topics) topic"
                        " where processed_at is not null group by topic"
                    )
                )
            ).all()
        )
        topics = []
        for name in names:
            rows = (
                await s.execute(
                    text(
                        "select id, title, source_type, feed_name, vertical, personal_score,"
                        " extract(epoch from (now()-coalesce(published_at, fetched_at)))::int age_s"
                        " from news_items where watch_topics @> array[:t] and processed_at is not null"
                        " order by fetched_at desc limit :lim"
                    ),
                    {"t": name, "lim": per_topic},
                )
            ).all()
            topics.append(
                {
                    "name": name,
                    "count": int(counts.get(name, 0)),
                    "items": [
                        {
                            "id": r.id,
                            "title": r.title,
                            "bucket": _bucket(r.source_type, r.feed_name),
                            "feed": r.feed_name,
                            "vertical": r.vertical,
                            "score": r.personal_score,
                            "age_s": r.age_s,
                        }
                        for r in rows
                    ],
                }
            )
    return {"topics": topics}


async def skipped_snapshot(limit: int = 60) -> dict:
    """Что отсеял гейт: счётчик + примеры с причиной. Чтобы отсев был виден, а не молчал."""
    async with SessionFactory() as s:
        total = (
            await s.execute(text("select count(*) from news_items where skip_reason is not null"))
        ).scalar() or 0
        last_hour = (
            await s.execute(
                text(
                    "select count(*) from news_items where skip_reason is not null"
                    " and processed_at > now() - interval '60 min'"
                )
            )
        ).scalar() or 0
        rows = (
            await s.execute(
                text(
                    "select id, title, source_type, feed_name, skip_reason,"
                    " extract(epoch from (now()-coalesce(published_at, fetched_at)))::int age_s"
                    " from news_items where skip_reason is not null"
                    " order by processed_at desc limit :lim"
                ),
                {"lim": limit},
            )
        ).all()
        by_feed = (
            await s.execute(
                text(
                    "select coalesce(feed_name, source_type) src, count(*) n from news_items"
                    " where skip_reason is not null group by 1 order by 2 desc limit 8"
                )
            )
        ).all()
    return {
        "total": total,
        "last_hour": last_hour,
        "by_feed": [{"feed": f, "n": n} for f, n in by_feed],
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "bucket": _bucket(r.source_type, r.feed_name),
                "feed": r.feed_name,
                "reason": r.skip_reason,
                "age_s": r.age_s,
            }
            for r in rows
        ],
    }


async def interests_snapshot() -> dict:
    """Три слоя «что мне нравится» разом: декларация · выученное · веса.

    Смысл панели — чтобы место, где задаются интересы, было видно, а не искалось по шести
    точкам. Отдельно показываем дрейф файла и БД: `medical: mute`, выкосивший 21% вертикали,
    прожил трое суток именно потому, что расхождение негде было увидеть.
    """
    import json as _json

    from homyak.core.interests import diff_declaration, load_interests

    inter = load_interests()
    async with SessionFactory() as s:
        profiles = (
            await s.execute(
                text(
                    "select vertical, version, description, topics::text, created_at"
                    " from profile where active order by vertical"
                )
            )
        ).all()
        muted = (
            await s.execute(text("select vertical, tag from muted_tags order by vertical, tag"))
        ).all()
        learned = (
            await s.execute(
                text(
                    "select vertical, tag, weight, n_pos, n_neg from tag_affinity"
                    " where n_pos > 0 or n_neg > 0 order by weight desc limit 24"
                )
            )
        ).all()
        taste = (await s.execute(text("select vertical, n_liked from taste_state"))).all()
        feedback_n = (await s.execute(text("select count(*) from feedback"))).scalar() or 0

    db = {r.vertical: r for r in profiles}
    verticals = []
    for name, decl in inter.verticals.items():
        row = db.get(name)
        drift = (
            diff_declaration(decl, row.description, _json.loads(row.topics)) if row else ["нет в БД"]
        )
        by_pol: dict[str, list[str]] = {}
        for t in decl.topics:
            by_pol.setdefault(t.polarity, []).append(t.name)
        verticals.append(
            {
                "vertical": name,
                "version": row.version if row else None,
                "description": " ".join((decl.description or "").split()),
                "topics": by_pol,
                "drift": drift,
            }
        )

    return {
        "verticals": verticals,
        "muted": [{"vertical": v, "tag": t} for v, t in muted],
        "learned": [
            {"vertical": v, "tag": t, "weight": round(w, 2), "n_pos": p, "n_neg": n}
            for v, t, w, p, n in learned
        ],
        "taste": [{"vertical": v, "n_liked": n} for v, n in taste],
        "feedback_n": feedback_n,
        "weights": inter.weights.model_dump(),
        "watch_n": len(inter.watch),
    }


async def item_detail(item_id: int) -> dict:
    """Полная карточка айтема для просмотра исходного сообщения."""
    async with SessionFactory() as s:
        it = await s.get(NewsItem, item_id)
    if it is None:
        return {}
    return {
        "id": it.id,
        "title": it.title,
        "text": it.text,
        "url": it.url,
        "source_type": it.source_type,
        "feed": it.feed_name,
        "author": it.author,
        "bucket": _bucket(it.source_type, it.feed_name),
        "vertical": it.vertical,
        "tags": list(it.tags or []),
        "watch": list(it.watch_topics or []),
        "summary": it.summary,
        "score": it.personal_score,
        "insight": it.insight_score,
        "processed": it.processed_at is not None,
    }


async def feed_snapshot(kind: str = "all", limit: int = 50) -> dict:
    """Персистентная лента «что получаем» + текущий фидбек по каждому айтему (для 👍/👎).

    Сортировка по СВЕЖЕСТИ, а не personal_score: иначе 45% твитов без вертикали
    (personal_score=NULL) тонут и их не видно. kind: all|twitter|business|it|medical|watch.
    """
    conds = ["processed_at is not null", "skip_reason is null"]
    params: dict = {"lim": max(1, min(limit, 200))}
    if kind == "twitter":
        conds.append("feed_name like 'tw_%'")
    elif kind in ("business", "it", "medical"):
        conds.append("vertical = :v")
        params["v"] = kind
    elif kind == "watch":
        conds.append("cardinality(watch_topics) > 0")
    where = " and ".join(conds)
    async with SessionFactory() as s:
        rows = (
            await s.execute(
                text(
                    "select id, title, source_type, feed_name, vertical, personal_score,"
                    " tags, watch_topics, url,"
                    " extract(epoch from (now()-coalesce(published_at,fetched_at)))::int age_s"
                    f" from news_items where {where}"
                    " order by coalesce(published_at, fetched_at) desc limit :lim"
                ),
                params,
            )
        ).all()
        ids = [r.id for r in rows]
        fb: dict[int, list[str]] = {}
        if ids:
            frows = (
                await s.execute(
                    text("select news_item_id, signal from feedback where news_item_id in :ids")
                    .bindparams(bindparam("ids", value=ids, expanding=True))
                )
            ).all()
            for iid, sig in frows:
                fb.setdefault(iid, []).append(sig)
    return {
        "kind": kind,
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "bucket": _bucket(r.source_type, r.feed_name),
                "feed": r.feed_name,
                "vertical": r.vertical,
                "score": r.personal_score,
                "tags": list(r.tags or [])[:4],
                "watch": list(r.watch_topics or []),
                "age_s": r.age_s,
                "feedback": fb.get(r.id, []),
            }
            for r in rows
        ],
    }


async def razbor_snapshot(item_id: int, repo) -> dict:
    """📝 Разбор для модалки дашборда — та же логика, что кнопка бота (общий core.razbor)."""
    from homyak.core.article import fetch_article
    from homyak.core.razbor import build_razbor
    from homyak.core.textutils import strip_html

    item = await repo.get_by_id(item_id)
    if item is None:
        return {"error": "нет такой статьи"}
    body_text = strip_html(item.text) or ""
    if len(body_text) < 400 and item.url and item.source_type != "telegram":
        fetched = await fetch_article(item.url)
        if fetched and len(fetched) > len(body_text):
            body_text = fetched
            await repo.set_item_text(item_id, fetched)
    if not body_text:
        return {"error": "нет текста для разбора"}
    return {"title": item.title, "url": item.url, "body": await build_razbor(item.title, body_text)}


async def stream_events(request: Request) -> EventSourceResponse:
    async def gen():
        nc = await nats.connect(settings.nats_url)
        js = nc.jetstream()
        # один эфемерный consumer на оба subject'а (ingested + processed), только новые события
        sub = await js.subscribe(
            "homyak.items.*", config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW)
        )
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await sub.next_msg(timeout=25)
                except TimeoutError:
                    yield {"event": "keepalive", "data": ""}
                    continue
                except Exception as e:
                    # НЕ keepalive+continue на любой ошибке: при закрытом NATS next_msg кидает
                    # мгновенно (а не по таймауту) → горячий цикл жёг ядро и заливал клиента.
                    log.info("sse_stream_stopped", error=str(e))
                    break
                await msg.ack()
                data = json.loads(msg.data)

                if msg.subject == SUBJECT_INGESTED:
                    payload = {"kind": "ingested", "source_type": data.get("source_type") or "rss"}
                    yield {"event": "flow", "data": json.dumps(payload, ensure_ascii=False)}
                    continue

                if msg.subject == SUBJECT_PROCESSED:
                    async with SessionFactory() as s:
                        it = await s.get(NewsItem, data["news_item_id"])
                    if it is None:
                        continue
                    payload = {
                        "kind": "processed",
                        "id": it.id,
                        "title": it.title,
                        "url": it.url,
                        "vertical": it.vertical,
                        "score": it.personal_score,
                        "feed": it.feed_name,
                        "bucket": _bucket(it.source_type, it.feed_name),
                        "tags": list(it.tags or [])[:3],
                        "watch": list(it.watch_topics or []),
                    }
                    yield {"event": "flow", "data": json.dumps(payload, ensure_ascii=False)}
        finally:
            await sub.unsubscribe()
            await nc.close()

    return EventSourceResponse(gen())


# --- HTML страница (self-contained, тёмная тема) ---

PAGE = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>🐹 Homyak — Live Pipeline</title>
<style>
:root{
  --bg:#0b0e14; --panel:#141924; --panel2:#1b2230; --line:#242c3d;
  --txt:#e6e9ef; --dim:#8a93a6; --accent:#6ee7ff;
  --biz:#f5b942; --it:#5aa9ff; --med:#4fd6a0; --tw:#7db9ff; --rss:#ff9b6a; --tg:#a78bfa;
}
*{box-sizing:border-box} html,body{margin:0;max-width:100%;overflow-x:hidden}
body{background:var(--bg);color:var(--txt);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration:none}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(11,14,20,.9);backdrop-filter:blur(8px);z-index:5}
header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.3px}
.dot{width:9px;height:9px;border-radius:50%;background:#f55;box-shadow:0 0 8px #f55;transition:.3s}
.dot.live{background:#4fd6a0;box-shadow:0 0 10px #4fd6a0}
.spacer{flex:1}
.clock{color:var(--dim);font-variant-numeric:tabular-nums}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px;padding:16px 20px;max-width:1400px;margin:0 auto}
.main,aside{min-width:0}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:14px}
@media(max-width:1100px){.cards{grid-template-columns:repeat(3,1fr)}}
@media(max-width:640px){.cards{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.card .v{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px}
.card .v small{font-size:12px;color:var(--dim);font-weight:500}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.panel h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);margin:0 0 10px}
#flow{display:flex;flex-direction:column;gap:8px;max-height:72vh;overflow:auto}
.row{display:flex;align-items:center;gap:10px;background:var(--panel2);border:1px solid var(--line);border-left-width:3px;border-radius:9px;padding:9px 11px;animation:in .35s ease}
@keyframes in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.row.biz{border-left-color:var(--biz)} .row.it{border-left-color:var(--it)} .row.med{border-left-color:var(--med)} .row.none{border-left-color:var(--line)}
.badge{flex:none;font-size:11px;padding:2px 7px;border-radius:6px;font-weight:600;white-space:nowrap}
.b-tw{background:rgba(125,185,255,.16);color:var(--tw)} .b-rss{background:rgba(255,155,106,.16);color:var(--rss)}
.b-tg{background:rgba(167,139,250,.16);color:var(--tg)} .b-other{background:#2a3242;color:var(--dim)}
.vpill{flex:none;font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.v-business{background:rgba(245,185,66,.15);color:var(--biz)} .v-it{background:rgba(90,169,255,.15);color:var(--it)} .v-medical{background:rgba(79,214,160,.15);color:var(--med)}
.ttl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sc{flex:none;font-variant-numeric:tabular-nums;font-weight:700;color:var(--accent)}
.bar{display:flex;height:26px;border-radius:8px;overflow:hidden;border:1px solid var(--line);margin-bottom:6px}
.bar>span{display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#0b0e14;min-width:22px;transition:flex .5s}
.seg-business{background:var(--biz)} .seg-it{background:var(--it)} .seg-medical{background:var(--med)}
.legend{display:flex;gap:14px;color:var(--dim);font-size:12px;flex-wrap:wrap}
.legend b{color:var(--txt)}
.srclist,.feedlist{display:flex;flex-direction:column;gap:7px}
.srcrow{display:flex;align-items:center;gap:9px}
.srcrow .n{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:700}
.mini{height:7px;border-radius:4px;background:#2a3242;flex:1;overflow:hidden}
.mini>i{display:block;height:100%;border-radius:4px}
.feedlist .srcrow{font-size:13px}
.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--rss);margin-left:6px;opacity:0}
.pulse.on{animation:p .6s ease}
@keyframes p{0%{opacity:1;transform:scale(1.6)}100%{opacity:0;transform:scale(1)}}
.muted{color:var(--dim);font-size:12px;text-align:center;padding:14px}
.row,.qrow{cursor:pointer} .row:hover,.qrow:hover{background:#212a3a}
.qrow .ttl{font-size:12.5px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.62);display:none;align-items:center;justify-content:center;z-index:20;padding:18px}
.modal.on{display:flex}
.mbox{background:var(--panel);border:1px solid var(--line);border-radius:14px;max-width:680px;width:100%;max-height:86vh;overflow:auto;padding:22px 22px 18px;position:relative}
.mclose{position:absolute;top:10px;right:12px;background:none;border:none;color:var(--dim);font-size:20px;cursor:pointer;line-height:1}
.mbox h3{margin:0 0 10px;font-size:17px;padding-right:26px;line-height:1.3}
.mmeta{color:var(--dim);font-size:12px;display:flex;gap:12px;flex-wrap:wrap;margin:4px 0}
.mtext{white-space:pre-wrap;word-break:break-word;color:var(--txt);background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:12px;margin:10px 0;font-size:13px;line-height:1.55}
.mlbl{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-top:8px}
.cardbtn{cursor:pointer;transition:border-color .15s} .cardbtn:hover{border-color:var(--accent)}
.qlist{display:flex;flex-direction:column;gap:6px;margin-top:10px}
.mback{display:inline-block;color:var(--accent);cursor:pointer;font-size:12px;margin-bottom:10px}
.watch{flex:none;font-size:11px;padding:2px 7px;border-radius:6px;font-weight:700;background:rgba(255,110,110,.16);color:#ff8f8f}
.wlwrap{max-width:1400px;margin:0 auto;padding:0 20px 26px}
.wlwrap>h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);margin:0 0 10px}
.wlgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.wlcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 13px;min-width:0}
/* Персистентная лента с фидбеком: видно всё (включая твиты), 👍/👎 обучают систему. */
.feedwrap{margin:14px 18px}
.feedwrap h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);margin:0 0 10px}
.ftabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.ftab{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:5px 11px;font-size:12px;color:var(--dim);cursor:pointer;user-select:none}
.ftab.on{background:var(--accent);border-color:var(--accent);color:#fff}
.frow{display:flex;align-items:center;gap:9px;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:8px 11px;margin-bottom:6px;min-width:0}
.frow .ttl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;color:var(--txt)}
.frow .ttl:hover{color:var(--accent)}
.frow .sc{color:var(--dim);font-size:12px;white-space:nowrap}
.vbtn{border:1px solid var(--line);background:var(--panel2);border-radius:7px;padding:3px 8px;cursor:pointer;font-size:13px;line-height:1;user-select:none}
.vbtn.up.on{background:#1c3a24;border-color:#2f7d4a}
.vbtn.down.on{background:#3a1c1c;border-color:#7d2f2f}
.vbtn.save.on{background:#3a331c;border-color:#7d6a2f}
/* Панель «Что мне нравится»: слои разделены визуально — в этом весь смысл панели. */
.ilayer{margin:16px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line);font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim)}
.ivert{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 12px;margin-bottom:8px}
.ivert h4{margin:0 0 6px;font-size:13px}
.idesc{color:var(--dim);font-size:12px;line-height:1.5;margin-bottom:8px}
.irow{display:flex;gap:10px;align-items:baseline;font-size:12px;padding:3px 0;min-width:0}
.ipol{flex:0 0 108px;color:var(--dim);white-space:nowrap}
.irow>span:last-child{min-width:0;word-break:break-word}
.idrift{margin-top:8px;padding:6px 8px;border-radius:6px;background:#3a2a10;border:1px solid #6b4a15;color:#f0b849;font-size:11px}
.wlcard h3{margin:0 0 8px;font-size:14px;display:flex;align-items:center;gap:8px}
.wlcard h3 .cnt{margin-left:auto;font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums}
.wlcard .il{display:flex;flex-direction:column;gap:5px}
.wlitem{display:flex;align-items:center;gap:7px;cursor:pointer;font-size:12.5px;padding:3px 0;border-radius:6px}
.wlitem:hover{background:#212a3a}
.wlitem .ttl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wlitem .n{flex:none;color:var(--dim);font-size:11px}
</style></head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>🐹 Homyak · Live Pipeline</h1>
  <span class="pulse" id="pulse"></span>
  <div class="spacer"></div>
  <div class="clock" id="clock"></div>
</header>

<div class="wrap">
  <div class="main">
    <div class="cards">
      <div class="card"><div class="k">Обработано</div><div class="v" id="c-proc">–</div></div>
      <div class="card"><div class="k">За час</div><div class="v" id="c-hour">–</div></div>
      <div class="card"><div class="k">Скорость</div><div class="v" id="c-rate">–<small>/мин</small></div></div>
      <div class="card cardbtn" onclick="openQueue()"><div class="k">В очереди ▸</div><div class="v" id="c-pend">–</div></div>
      <div class="card cardbtn" onclick="openSkipped()"><div class="k">🚫 Отсеяно ▸</div><div class="v" id="c-skip">–</div></div>
      <div class="card"><div class="k">Пушей</div><div class="v" id="c-push">–</div></div>
      <div class="card cardbtn" onclick="openInterests()"><div class="k">👤 Что мне нравится ▸</div><div class="v" style="font-size:15px">3 слоя</div></div>
    </div>
    <div class="panel">
      <h2>Поток в реальном времени · источник → вертикаль → score</h2>
      <div id="flow"><div class="muted">Ждём событий…</div></div>
    </div>
  </div>

  <aside>
    <div class="panel">
      <h2>По вертикалям</h2>
      <div class="bar" id="vbar"></div>
      <div class="legend" id="vleg"></div>
    </div>
    <div class="panel">
      <h2>По источникам (обработано)</h2>
      <div class="srclist" id="srclist"><div class="muted">…</div></div>
    </div>
    <div class="panel">
      <h2>Топ фидов (за час)</h2>
      <div class="feedlist" id="feedlist"><div class="muted">…</div></div>
    </div>
    <div class="panel">
      <h2>Сессия (с открытия)</h2>
      <div class="legend"><span>📥 inflow <b id="s-in">0</b></span><span>⚙️ processed <b id="s-proc">0</b></span></div>
    </div>
  </aside>
</div>

<div class="feedwrap">
  <h2>🗂 Лента — что получаем · 👍/👎 обучают систему</h2>
  <div class="ftabs" id="ftabs"></div>
  <div id="lentafeed"><div class="muted">…</div></div>
</div>

<div class="wlwrap">
  <h2>👁 Watchlist · трендовые темы под пристальным вниманием</h2>
  <div class="wlgrid" id="wlgrid"><div class="muted">…</div></div>
</div>

<div class="modal" id="modal"><div class="mbox" id="mbox">
  <button class="mclose" id="mclose">✕</button>
  <div id="mbody"></div>
</div></div>

<script>
const $=id=>document.getElementById(id);
// Экранируем ВСЁ, включая кавычки: esc() попадает и в атрибуты (href/title), где
// незакрытая кавычка = выход из атрибута = XSS. Заголовки/URL приходят из чужих RSS.
const esc=s=>(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
// href принимаем только http(s): 'javascript:' из чужого фида иначе выполнится по клику.
const safeUrl=u=>{try{const p=new URL(u).protocol;return (p==='http:'||p==='https:')?u:null;}catch(e){return null;}};
const fmtAge=s=>{s=Math.max(0,s|0);return s<60?s+'с':s<3600?Math.floor(s/60)+'м':Math.floor(s/3600)+'ч';};
const VCOLOR={business:'--biz',it:'--it',medical:'--med'};
const VLABEL={business:'💼 Business',it:'💻 IT',medical:'🩺 Medical'};
const BADGE={twitter:['b-tw','🐦'],rss:['b-rss','📡'],telegram:['b-tg','✈️'],other:['b-other','•']};
let sIn=0,sProc=0;

function clock(){const d=new Date();$('clock').textContent=d.toLocaleTimeString('ru-RU');}
setInterval(clock,1000);clock();

function pulse(){const p=$('pulse');p.classList.remove('on');void p.offsetWidth;p.classList.add('on');}

function addRow(m){
  const flow=$('flow');
  if(flow.firstChild&&flow.firstChild.className==='muted')flow.innerHTML='';
  const row=document.createElement('div');
  row.className='row '+(m.vertical?({business:'biz',it:'it',medical:'med'}[m.vertical]||'none'):'none');
  const [bc,be]=BADGE[m.bucket]||BADGE.other;
  const acc=esc(m.feed&&m.feed.startsWith('tw_')?'@'+m.feed.slice(3):(m.feed||m.bucket));
  const vp=m.vertical?`<span class="vpill v-${m.vertical}">${VLABEL[m.vertical]||m.vertical}</span>`:'';
  const sc=m.score!=null?`<span class="sc">${Math.round(m.score*100)}%</span>`:'';
  const wb=(m.watch&&m.watch.length)?`<span class="watch">👁 ${esc(m.watch.join(', '))}</span>`:'';
  row.innerHTML=`<span class="badge ${bc}">${be} ${acc}</span>${vp}${wb}<span class="ttl">${esc(m.title||'—')}</span>${sc}`;
  row.onclick=()=>openItem(m.id);
  flow.prepend(row);
  while(flow.children.length>60)flow.removeChild(flow.lastChild);
}

function renderStats(s){
  $('c-proc').textContent=s.processed.toLocaleString('ru-RU');
  $('c-hour').textContent=s.last_hour.toLocaleString('ru-RU');
  $('c-rate').innerHTML=(s.last_10m/10).toFixed(1)+'<small>/мин</small>';
  $('c-pend').textContent=s.pending.toLocaleString('ru-RU');
  $('c-push').textContent=s.pushed.toLocaleString('ru-RU');
  $('c-skip').textContent=(s.skipped||0).toLocaleString('ru-RU');
  // bar по вертикалям
  const bv=s.by_vertical, tot=Object.values(bv).reduce((a,b)=>a+b,0)||1;
  const order=['business','it','medical'];
  $('vbar').innerHTML=order.map(v=>{const n=bv[v]||0;const pc=Math.max(n/tot*100,n?6:0);
    return `<span class="seg-${v}" style="flex:${pc}">${n||''}</span>`;}).join('');
  $('vleg').innerHTML=order.map(v=>`<span style="color:var(${VCOLOR[v]})">●</span> <b>${(bv[v]||0).toLocaleString('ru-RU')}</b> ${VLABEL[v]}`).join('');
  // источники
  const bs=s.by_source, smax=Math.max(...Object.values(bs),1);
  $('srclist').innerHTML=Object.entries(bs).map(([k,n])=>{
    const [bc,be]=BADGE[k]||BADGE.other;const col=k==='twitter'?'--tw':k==='telegram'?'--tg':'--rss';
    return `<div class="srcrow"><span class="badge ${bc}">${be} ${esc(k)}</span><span class="mini"><i style="width:${n/smax*100}%;background:var(${col})"></i></span><span class="n">${n.toLocaleString('ru-RU')}</span></div>`;
  }).join('')||'<div class="muted">нет данных</div>';
  // топ фидов
  const fmax=(s.top_feeds[0]?.n)||1;
  $('feedlist').innerHTML=s.top_feeds.map(f=>{const nm=esc(f.feed.startsWith('tw_')?'🐦 @'+f.feed.slice(3):f.feed);
    return `<div class="srcrow"><span class="ttl">${nm}</span><span class="mini"><i style="width:${f.n/fmax*100}%;background:var(--accent)"></i></span><span class="n">${f.n}</span></div>`;
  }).join('')||'<div class="muted">за час тихо</div>';
}

async function pollStats(){try{const r=await fetch('/dashboard/stats');renderStats(await r.json());}catch(e){}}
pollStats();setInterval(pollStats,12000);

async function pollWatchlist(){try{
  const w=await (await fetch('/dashboard/watchlist')).json();
  $('wlgrid').innerHTML=w.topics.map(t=>{
    const items=t.items.map(it=>{const [bc,be]=BADGE[it.bucket]||BADGE.other;
      return `<div class="wlitem" onclick="openItem(${it.id})"><span class="badge ${bc}">${be}</span><span class="ttl">${esc(it.title||'—')}</span><span class="n">${fmtAge(it.age_s)}</span></div>`;
    }).join('')||'<div class="muted">пока пусто</div>';
    return `<div class="wlcard"><h3>👁 ${esc(t.name)}<span class="cnt">${t.count.toLocaleString('ru-RU')}</span></h3><div class="il">${items}</div></div>`;
  }).join('')||'<div class="muted">вотчлист пуст</div>';
}catch(e){}}
pollWatchlist();setInterval(pollWatchlist,15000);

// --- Лента с фидбеком ---
const FTABS=[['all','Все'],['twitter','🐦 Twitter'],['business','💼'],['it','💻'],['medical','🩺'],['watch','👁']];
let feedKind='all';
function renderFtabs(){$('ftabs').innerHTML=FTABS.map(([k,l])=>`<span class="ftab ${k===feedKind?'on':''}" onclick="setFeedKind('${k}')">${l}</span>`).join('');}
function setFeedKind(k){feedKind=k;renderFtabs();loadFeed();}
async function loadFeed(){try{
  const d=await (await fetch('/dashboard/feed?kind='+feedKind+'&limit=60')).json();
  $('lentafeed').innerHTML=(d.items||[]).map(it=>{
    const [bc,be]=BADGE[it.bucket]||BADGE.other;
    const nm=it.feed&&it.feed.startsWith('tw_')?'@'+it.feed.slice(3):(it.feed||it.vertical||it.bucket);
    const sc=it.score!=null?Math.round(it.score*100)+'%':'—';
    const fb=it.feedback||[];const on=s=>fb.includes(s)?'on':'';
    return `<div class="frow">
      <span class="badge ${bc}">${be} ${esc(nm)}</span>
      <span class="ttl" onclick="openItem(${it.id})" title="${esc(it.title||'')}">${esc(it.title||'—')}</span>
      <span class="sc">🎯 ${sc}</span>
      <span class="vbtn up ${on('up')}" onclick="vote(${it.id},'up',this)">👍</span>
      <span class="vbtn down ${on('down')}" onclick="vote(${it.id},'down',this)">👎</span>
      <span class="vbtn save ${on('save')}" onclick="vote(${it.id},'save',this)">⭐</span>
    </div>`;
  }).join('')||'<div class="muted">пусто</div>';
}catch(e){}}
async function vote(id,signal,el){
  const was=el.classList.contains('on');
  el.classList.toggle('on');  // оптимистично
  try{
    const r=await (await fetch('/dashboard/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:id,signal})})).json();
    el.classList.toggle('on', r.action==='added');  // сервер — источник истины
  }catch(e){el.classList.toggle('on', was);}  // откат при ошибке
}
async function openRazbor(id){
  const box=$('razbor-'+id); if(!box)return;
  box.innerHTML='<div class="muted">Разбираю… (~7с)</div>';
  try{
    const d=await (await fetch('/dashboard/razbor/'+id)).json();
    if(d.error){box.innerHTML='<div class="muted">'+esc(d.error)+'</div>';return;}
    const html=(d.body||'').split('\n').map(l=>{l=l.trim();if(!l)return '';
      return (l.startsWith('**')&&l.endsWith('**'))?'<div class="mlbl">'+esc(l.slice(2,-2))+'</div>':'<div>'+esc(l)+'</div>';}).join('');
    box.innerHTML='<div class="mtext">'+html+'</div>';
  }catch(e){box.innerHTML='<div class="muted">ошибка разбора</div>';}
}
renderFtabs();loadFeed();setInterval(loadFeed,20000);

let mStack=[];
function mHide(){$('modal').classList.remove('on');mStack=[];}
function mBack(){
  if(mStack.length){const s=mStack.pop();$('mbody').innerHTML=s.html;const b=$('mbox');if(b)requestAnimationFrame(()=>{b.scrollTop=s.scroll;});}
  else mHide();
}

const POL={love:['❤️','обожаю'],like:['👍','нравится'],meh:['😐','нейтрально'],dislike:['👎','не люблю'],mute:['🔇','мьют']};

// Панель существует ради одного: чтобы «где задаётся, что мне нравится» было ВИДНО.
// Слои показаны раздельно и в том же порядке, что в config/interests.yaml и в CLI.
async function openInterests(){try{
  mStack=[];
  $('mbody').innerHTML='<div class="muted">Загружаю…</div>';
  $('modal').classList.add('on');
  const d=await (await fetch('/dashboard/interests')).json();
  let h='<h3>👤 Что мне нравится</h3>';
  h+='<div class="mmeta"><span>задаётся в <b>config/interests.yaml</b></span><span>применить: <b>homyak-interests apply</b></span></div>';

  h+='<div class="ilayer"><b>1 · ДЕКЛАРАЦИЯ</b> — твои слова, из файла</div>';
  h+=d.verticals.map(v=>{
    const drift=v.drift&&v.drift.length
      ? `<div class="idrift">≠ файл разошёлся с БД: ${v.drift.map(x=>esc(x)).join(' · ')}</div>` : '';
    const tp=Object.keys(POL).filter(p=>v.topics[p]&&v.topics[p].length)
      .map(p=>`<div class="irow"><span class="ipol">${POL[p][0]} ${POL[p][1]}</span><span>${esc(v.topics[p].join(', '))}</span></div>`).join('');
    return `<div class="ivert"><h4>${esc(v.vertical)} <span class="cnt">v${v.version??'—'}</span></h4>`
         + `<div class="idesc">${esc(v.description)}</div>${tp}${drift}</div>`;
  }).join('');

  h+='<div class="ilayer"><b>2 · ВЫУЧЕННОЕ</b> — система сама, из 👍/👎. В файл не пишет</div>';
  const taste=d.taste.map(t=>`${esc(t.vertical)}: ${t.n_liked}`).join(', ')||'—';
  h+=`<div class="mmeta"><span>фидбеков всего: <b>${d.feedback_n}</b></span><span>вкус (n_liked): <b>${taste}</b></span></div>`;
  h+= d.muted.length
    ? '<div class="irow"><span class="ipol">🔇 кнопкой</span><span>'+d.muted.map(m=>esc(m.vertical+'/'+m.tag)).join(', ')+'</span></div>'
    : '<div class="irow"><span class="ipol">🔇 кнопкой</span><span class="muted">пусто</span></div>';
  h+= d.learned.length
    ? '<div class="qlist">'+d.learned.map(l=>`<div class="srcrow"><span class="badge b-rss">${esc(l.vertical)}</span><span class="ttl">${esc(l.tag)}</span><span class="n">${l.weight>0?'+':''}${l.weight} <small>(${l.n_pos}👍/${l.n_neg}👎)</small></span></div>`).join('')+'</div>'
    : '<div class="muted">обучение пока ничего не накопило</div>';

  h+='<div class="ilayer"><b>3 · ВЕСА</b> — сколько что значит</div>';
  h+='<div class="qlist">'+Object.entries(d.weights).map(([k,v])=>
      `<div class="srcrow"><span class="ttl">${esc(k)}</span><span class="n">${v}</span></div>`).join('')+'</div>';
  h+=`<div class="mmeta"><span>под наблюдением (watch): <b>${d.watch_n}</b> тем</span></div>`;
  $('mbody').innerHTML=h;
}catch(e){$('mbody').innerHTML='<div class="muted">ошибка загрузки</div>';}}

async function openSkipped(){try{
  mStack=[];
  $('mbody').innerHTML='<div class="muted">Загружаю…</div>';
  $('modal').classList.add('on');
  const q=await (await fetch('/dashboard/skipped?limit=100')).json();
  let h=`<h3>🚫 Отсеяно гейтом · ${q.total.toLocaleString('ru-RU')}</h3>`;
  h+=`<div class="mmeta">за час: ${q.last_hour} · до LLM не дошли (сэкономлено ~${(q.last_hour*2).toLocaleString('ru-RU')} вызовов) · клик — исходник</div>`;
  if(q.by_feed&&q.by_feed.length){
    h+='<div class="mmeta">'+q.by_feed.map(f=>`<span>${esc(f.feed||'?')}: <b>${f.n}</b></span>`).join('')+'</div>';
  }
  h+='<div class="qlist">'+(q.items.map(it=>{
    const [bc,be]=BADGE[it.bucket]||BADGE.other;
    const nm=it.feed&&it.feed.startsWith('tw_')?'@'+it.feed.slice(3):(it.feed||it.bucket);
    const sim=(it.reason||'').match(/0\.[0-9]+/);
    return `<div class="srcrow qrow" onclick="openItem(${it.id},1)"><span class="badge ${bc}">${be} ${esc(nm)}</span><span class="ttl">${esc(it.title||'—')}</span><span class="n" title="${esc(it.reason||'')}">${sim?sim[0]:''}</span></div>`;
  }).join('')||'<div class="muted">гейт пока ничего не отсёк</div>')+'</div>';
  $('mbody').innerHTML=h;
}catch(e){$('mbody').innerHTML='<div class="muted">ошибка загрузки</div>';}}

async function openQueue(){try{
  mStack=[];
  $('mbody').innerHTML='<div class="muted">Загружаю очередь…</div>';
  $('modal').classList.add('on');
  const q=await (await fetch('/dashboard/queue?limit=200')).json();
  let h=`<h3>⏳ Очередь на обработку · ${q.pending.toLocaleString('ru-RU')}</h3>`;
  h+=`<div class="mmeta">старейшие сверху · возраст = когда новость вышла (·у нас = сколько ждёт обработки) · клик — исходник${q.pending>q.items.length?' · показаны первые '+q.items.length:''}</div>`;
  h+='<div class="qlist">'+(q.items.map(it=>{
    const [bc,be]=BADGE[it.bucket]||BADGE.other;
    const nm=it.feed&&it.feed.startsWith('tw_')?'@'+it.feed.slice(3):(it.feed||it.bucket);
    const lag = it.lag_s!=null?` <span style="opacity:.5">·у нас ${fmtAge(it.lag_s)}</span>`:'';
    return `<div class="srcrow qrow" onclick="openItem(${it.id},1)"><span class="badge ${bc}">${be} ${esc(nm)}</span><span class="ttl">${esc(it.title||'—')}</span><span class="n" title="возраст новости">${fmtAge(it.age_s)}${lag}</span></div>`;
  }).join('')||'<div class="muted">очередь пуста 🎉</div>')+'</div>';
  $('mbody').innerHTML=h;
}catch(e){$('mbody').innerHTML='<div class="muted">ошибка загрузки</div>';}}

async function openItem(id,fromQueue){try{
  const it=await (await fetch('/dashboard/item/'+id)).json();
  if(!it.id)return;
  if(fromQueue){mStack.push({html:$('mbody').innerHTML,scroll:$('mbox')?$('mbox').scrollTop:0});}
  const src=it.feed&&it.feed.startsWith('tw_')?'🐦 @'+it.feed.slice(3):(it.feed||it.source_type||'?');
  const meta=[src, it.vertical?VLABEL[it.vertical]:'',
    (it.watch&&it.watch.length)?'👁 '+it.watch.join(', '):'', it.processed?'✓ обработан':'⏳ в очереди',
    it.score!=null?'🎯 '+Math.round(it.score*100)+'%':'', it.insight!=null?'💡 '+Math.round(it.insight*100)+'%':''].filter(Boolean);
  let h=mStack.length?`<span class="mback" onclick="mBack()">← назад</span>`:'';
  h+=`<h3>${esc(it.title||'(без заголовка)')}</h3>`;
  h+=`<div class="mmeta">${meta.map(m=>'<span>'+esc(m)+'</span>').join('')}</div>`;
  if(it.tags&&it.tags.length)h+=`<div class="mmeta">${it.tags.map(t=>'#'+esc(t)).join(' ')}</div>`;
  h+=`<div class="mlbl">Исходное сообщение</div><div class="mtext">${esc(it.text)||'(нет текста)'}</div>`;
  if(it.summary)h+=`<div class="mlbl">Саммари</div><div class="mtext">${esc(it.summary)}</div>`;
  h+=`<div style="margin:12px 0 4px"><span class="vbtn" onclick="openRazbor(${it.id})">📝 Разбор</span></div><div id="razbor-${it.id}"></div>`;
  const u=safeUrl(it.url);
  if(u)h+=`<a href="${esc(u)}" target="_blank" rel="noopener" style="color:var(--accent)">🔗 Открыть оригинал</a>`;
  $('mbody').innerHTML=h;
  $('modal').classList.add('on');
}catch(e){}}
$('mclose').onclick=mBack;
$('modal').onclick=e=>{if(e.target.id==='modal')mBack();};
document.addEventListener('keydown',e=>{if(e.key==='Escape')mBack();});

let es;
function connect(){
  es=new EventSource('/dashboard/stream');
  es.onopen=()=>$('dot').classList.add('live');
  es.onerror=()=>$('dot').classList.remove('live');
  es.addEventListener('flow',ev=>{
    const m=JSON.parse(ev.data);
    if(m.kind==='ingested'){sIn++;$('s-in').textContent=sIn;pulse();return;}
    if(m.kind==='processed'){sProc++;$('s-proc').textContent=sProc;addRow(m);}
  });
}
connect();
</script>
</body></html>
"""
