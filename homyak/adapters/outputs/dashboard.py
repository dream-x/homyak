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
from sqlalchemy import text
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
                    " count(*) filter (where processed_at > now() - interval '10 min') last_10m"
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
        "by_vertical": {v: n for v, n in by_vertical},
        "by_source": {v: n for v, n in by_source},
        "top_feeds": [{"feed": f, "n": n} for f, n in top_feeds],
    }


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
                except Exception:
                    yield {"event": "keepalive", "data": ""}
                    continue
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
*{box-sizing:border-box} html,body{margin:0}
body{background:var(--bg);color:var(--txt);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration:none}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(11,14,20,.9);backdrop-filter:blur(8px);z-index:5}
header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.3px}
.dot{width:9px;height:9px;border-radius:50%;background:#f55;box-shadow:0 0 8px #f55;transition:.3s}
.dot.live{background:#4fd6a0;box-shadow:0 0 10px #4fd6a0}
.spacer{flex:1}
.clock{color:var(--dim);font-variant-numeric:tabular-nums}
.wrap{display:grid;grid-template-columns:1fr 320px;gap:16px;padding:16px 20px;max-width:1400px;margin:0 auto}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:14px}
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
      <div class="card"><div class="k">В очереди</div><div class="v" id="c-pend">–</div></div>
      <div class="card"><div class="k">Пушей</div><div class="v" id="c-push">–</div></div>
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

<script>
const $=id=>document.getElementById(id);
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
  const acc=m.feed&&m.feed.startsWith('tw_')?'@'+m.feed.slice(3):(m.feed||m.bucket);
  const vp=m.vertical?`<span class="vpill v-${m.vertical}">${VLABEL[m.vertical]||m.vertical}</span>`:'';
  const sc=m.score!=null?`<span class="sc">${Math.round(m.score*100)}%</span>`:'';
  row.innerHTML=`<span class="badge ${bc}">${be} ${acc}</span>${vp}<span class="ttl">${(m.title||'—').replace(/</g,'&lt;')}</span>${sc}`;
  if(m.url)row.title=m.url;
  flow.prepend(row);
  while(flow.children.length>60)flow.removeChild(flow.lastChild);
}

function renderStats(s){
  $('c-proc').textContent=s.processed.toLocaleString('ru-RU');
  $('c-hour').textContent=s.last_hour.toLocaleString('ru-RU');
  $('c-rate').innerHTML=(s.last_10m/10).toFixed(1)+'<small>/мин</small>';
  $('c-pend').textContent=s.pending.toLocaleString('ru-RU');
  $('c-push').textContent=s.pushed.toLocaleString('ru-RU');
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
    return `<div class="srcrow"><span class="badge ${bc}">${be} ${k}</span><span class="mini"><i style="width:${n/smax*100}%;background:var(${col})"></i></span><span class="n">${n.toLocaleString('ru-RU')}</span></div>`;
  }).join('')||'<div class="muted">нет данных</div>';
  // топ фидов
  const fmax=(s.top_feeds[0]?.n)||1;
  $('feedlist').innerHTML=s.top_feeds.map(f=>{const nm=f.feed.startsWith('tw_')?'🐦 @'+f.feed.slice(3):f.feed;
    return `<div class="srcrow"><span class="ttl">${nm}</span><span class="mini"><i style="width:${f.n/fmax*100}%;background:var(--accent)"></i></span><span class="n">${f.n}</span></div>`;
  }).join('')||'<div class="muted">за час тихо</div>';
}

async function pollStats(){try{const r=await fetch('/dashboard/stats');renderStats(await r.json());}catch(e){}}
pollStats();setInterval(pollStats,12000);

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
