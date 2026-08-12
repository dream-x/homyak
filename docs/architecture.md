# Homyak — architecture

**English** · [Русский](architecture.ru.md)

## Purpose

A personal news aggregator that merges heterogeneous sources (Telegram channels via **tscrapper→NATS**,
Miniflux RSS, RSS news sites) into one deduplicated **personal** feed with local LLM processing. The main
output is a **Telegram bot** with interest-based ranking, 👍/👎 reactions (learning), summaries and a
full-text reader (Telegraph). Plus CLI, REST/RSS/JSON feed and SSE.

Two architectural pillars:
- **Plugin adapter system** of three kinds: **sources**, **analyzers**, **outputs**. The core knows nothing
  about specific sources/analyzers/outputs.
- **Near-realtime event-driven pipeline** on **NATS JetStream** as the single internal bus. No DB polling,
  no Kafka.

> Status: implemented Phases 1-4 + all of personalization (Phase 6.0-6.3) + 3 topic verticals. The whole
> stack is containerized under **Podman**. Feature details in `docs/phase-*.md`.

## Stack

- Python 3.13, **uv** (packaging + lockfile `uv.lock`)
- FastAPI, SQLAlchemy 2.x (async), Alembic, asyncpg, pydantic-settings
- Postgres 17 (metadata + FTS), Qdrant (1024-dim vectors for bge-m3, collections `news_items` + `taste`)
- **NATS 2.10 + JetStream** (event bus)
- **Ollama** (on the host, Metal): `bge-m3` (embeddings), `qwen2.5:14b` (judge/tags),
  `gpt-oss:120b-cloud` → `gemma4` (summaries)
- feedparser + trafilatura (fetch/extract full article text), httpx; aiogram + Telegraph (bot); Telethon in tscrapper
- structlog
- **Deploy:** `Dockerfile` + `docker-compose.yml` (podman compose) — postgres/qdrant/nats + migrate +
  app services (ingest-poll, telegram-ingest, processor, learner, sweeper, tgbot, api). Ollama on the host
  (`host.containers.internal:11434`). `podman compose up -d` brings everything up.

---

## Event-driven pipeline (NATS JetStream)

**Stream** `HOMYAK` — file storage, retention `limits`, max_age 14d, max_bytes 5GB, 1 replica.

**Subjects:**
- `homyak.items.ingested` — from source adapters/consumers after `upsert_item()`. Payload `{news_item_id, source_type}`.
- `homyak.items.processed` — from the processor after all analyzer stages. Payload `{news_item_id, cluster_id, category}`.
- `homyak.telegram.raw` — raw messages from **tscrapper**. Consumer `telegram-ingest` upserts → `items.ingested`.
- `homyak.feedback.recorded` — 👍/👎/⭐/🔇 reactions from the bot. Consumer `learner` shifts taste/weights.
- `homyak.profile.suggestion` — profile-refinement proposal (from learner every N reactions) → the bot shows a card.

**Durable consumers:** `processor` (on `items.ingested`, pull, ack/nak+backoff, max_deliver=5), `learner`
(on `feedback.recorded`), `telegram-ingest` (on `telegram.raw`), `profile-suggest` (bot, on `profile.suggestion`).
The bot push and SSE are ephemeral consumers on `items.processed` (deliver=new).

**Fallback sweep:** every 5 min a sweep job finds `processed_at IS NULL AND fetched_at < now()-5min AND
attempts < 5` and re-publishes `items.ingested` — protects against event loss while NATS/processor is down.

---

## Telegram via tscrapper

**[tscrapper](https://github.com/maks/tscrapper)** is a **separate service** (its own repo, Telethon, own
Telegram user session) that monitors ~50 Telegram channels and forwards them to aggregated target channels.
It was extended with a best-effort NATS publisher: in `_handle_message` every message is also published to
`homyak.telegram.raw` when `NATS_URL` is set — **without affecting forwarding** (publish failures are swallowed).

On the Homyak side, `pipeline/telegram_ingest.py` (`homyak-telegram-ingest`) consumes `homyak.telegram.raw`,
validates the payload, `upsert_item`s it (idempotent via `(source_type='telegram', source_id)` UNIQUE) and
publishes `items.ingested`. From there the normal pipeline runs. tscrapper stays on the host (its session);
Homyak runs in containers and reaches the same NATS on `:4222`.

> A file-based `telegram_relay`/`tg_relay` (tail a JSONL outbox) remains as an alternative, but NATS is the
> primary path. tscrapper's channels are AI/tech-leaning, so they land mostly in the `it` vertical.

---

## Data model (Postgres)

Core: `news_items` (source_type/source_id UNIQUE, url/url_normalized, title/text, media, author, feed_name,
**vertical**, tags, category, summary, score, generated `search_tsv`; cluster_id FK; embedding_model/version;
processing state; **llm_relevance/llm_reason/personal_score/scored_profile_version/pushed_at**), `clusters`,
`ingest_state` (per-source cursor).

Personalization (per-vertical): `profile` (one active per vertical), `tag_affinity` (PK vertical+tag),
`source_affinity` (PK vertical+source_type+author), `feedback` (UNIQUE item+signal), `taste_state`
(PK vertical, n_liked). Migrations `0001`–`0007`.

**Qdrant:** collection `news_items` (1024-dim, Cosine, payload source_type/category/published_at) written by
`embedder`; collection `taste` — one point per vertical (the taste centroid).

**Idempotency:** `(source_type, source_id)` UNIQUE; `upsert_item()` uses `ON CONFLICT … DO UPDATE` and returns
`(id, was_new)`; `items.ingested` is published only when `was_new=true`.

---

## Processing pipeline (9 stages)

Analyzers run sequentially in the `processor` (mutating a shared `AnalyzerContext`):

| # | Stage | What it does |
|---|---|---|
| 0 | `article_fetch` | downloads the **full article text** by URL (trafilatura) if RSS gave only a stub |
| 1 | `url_dedup` | normalizes URL, finds/creates a cluster by normalized URL |
| 2 | `embedder` | bge-m3 via Ollama `/api/embed`, upsert to Qdrant, sets `embedding_model/version` |
| 3 | `similarity_dedup` | Qdrant top-5, threshold 0.88, merges clusters (advisory lock) |
| 4 | `llm_tagger` | tags + **vertical** (business/it/medical/other), `qwen2.5:14b`, JSON |
| 5 | `llm_summarizer` | mixed summary (gist + takeaways), engineer voice, original language. `gpt-oss:120b-cloud` → `gemma4` fallback |
| 6 | `scorer` | base `freshness · (1+raw) · (1+ln(cluster_size))` |
| 7 | `llm_relevance` | **LLM judge**: relevance to the item's vertical profile (0..1) + reason, cached by `scored_profile_version` |
| 8 | `personalizer` | hybrid `personal_score` (llm + taste + tag/source affinity + fresh) − hard-mute |

On success: `processed_at = now()`, publish `items.processed`. Back-pressure: JetStream `max_deliver=5` +
exponential backoff via `nak`, circuit breaker on Ollama.

---

## Topic verticals (business / it / medical)

The feed is split into **3 independent verticals** — each with its own interest profile, learning, taste
vector and feed. A like in IT does not affect medical.

- **Classification:** `llm_tagger` returns `vertical` in JSON. `other` doesn't enter any vertical
  (`personal_score = NULL`).
- **Per-vertical state** (migration 0007): `news_items.vertical`; `profile`, `tag_affinity`,
  `source_affinity`, `taste_state` are keyed by vertical (one active profile per vertical); the taste vector
  is a per-vertical point in the Qdrant `taste` collection.
- **Scoring:** `llm_relevance` judges a story against the profile of **its** vertical; `personalizer`
  computes `personal_score` from that vertical's affinities/taste; `learner` trains the item's vertical.
- **Interests:** `config/interests.yaml` is the single place a preference is ever declared → `homyak-interests
  apply`. One-way wall: the declaration seeds the learned layer, the learned layer never writes back.
  🔇 lands in `muted_tags`, not in the profile. Refinement only *proposes*; you accept it via the bot.
- **Outputs:** bot commands/buttons `/business /it /medical`, a vertical badge on each post; feed `?vertical=`.
- **Sources** are topical (WSJ/Economist… → business; STAT/Lancet… → medical; HN/arXiv… → it), but the
  vertical is decided by the tagger from content, not by the source.

---

## Personalization (the flagship)

`personal_score` (per vertical) is a hybrid blend:

```
personal_score = w_llm·llm_relevance          # LLM judge vs the vertical's profile (0..1, cached)
               + w_taste·cosine(item, taste)  # closeness to the taste vector (centroid of likes)
               + w_tag·tag_affinity           # learned per-tag weights (EMA)
               + w_source·source_affinity      # learned per-feed/channel weights (EMA)
               + w_fresh·freshness             # exponential recency decay
               − hard_mute                     # muted topics never enter the feed
```

**Learning loop:** the bot pushes stories above a threshold; 👍/👎/⭐ move `tag_affinity`/`source_affinity`
(EMA in `[-1..1]`) and the taste vector (incremental centroid of liked embeddings, reversible on toggle);
🔇 mutes a topic. Every N reactions the `learner` asks an LLM to propose a refined profile, delivered as a
bot card the user confirms (✅/❌). Cold-start: the text profile works from day one; taste weight ramps up
with the number of likes.

Expensive `llm_relevance` is cached per profile version; the light components (taste/tag/source/fresh) are
recomputed on read, so learning affects ranking without reprocessing.

---

## Plugin interfaces (`homyak/core/interfaces.py`)

- `PollSource` — `poll(cursor) -> AsyncIterator[(NewsItemDTO, cursor)]` (RSS, Miniflux); driven by APScheduler.
- `PushSource` — `subscribe(sink)` long-running (telegram relay).
- `Analyzer` — `analyze(ctx: AnalyzerContext)` mutates the shared context; ordered by `stage`.
- `OutputAdapter` — `serve(query: FeedQuery) -> Feed`.

`FeedQuery`: `category`, `source_types`, `feed_name`, `vertical`, `min_score`, `since`, `collapse_clusters`,
`sort` (recent/score/personal), `limit`, `cursor`. Adapters are wired in `core/registry.py`.

---

## Deploy & services

`podman compose up -d` starts postgres/qdrant/nats + a one-shot `migrate` + the app services. One image
(`Dockerfile`, uv), a command per service. `config/` is mounted read-only so source/profile edits need no
rebuild. Ollama stays on the host (Metal).

| Service | Role |
|---|---|
| `ingest-poll` | poll RSS/Miniflux (+ GitHub/Twitter via RSSHub) |
| `telegram-ingest` | consume `homyak.telegram.raw` (tscrapper) |
| `processor` | pipeline (url_dedup … title_gen … personalizer) |
| `learner` | learning + profile refinement |
| `sweeper` | re-publish stuck items |
| `tgbot` | Telegram bot |
| `api` | FastAPI (`/feed*`, `/saved*`, `/lenta`, `/search`, `/ask`, `/dashboard`) |
| `wiki` | LLM knowledge base from ⭐/👍 (below) |
| `starchan` | ⭐ channel: starred items + daily digest (below) |

Secrets live only in `.env` (gitignored): `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, scoring weights, thresholds,
`GITHUB_ACCESS_TOKEN` (RSSHub GitHub trending).

## Search (`/search` + bot `/find`)

Hybrid retrieval over the whole corpus, `core/search.py`:
- **Lexical** — Postgres FTS on the persisted `search_tsv` (`websearch_to_tsquery('simple', …)`, GIN index).
- **Semantic** — query embedded with bge-m3, Qdrant `search_similar`.
- **Fusion** — Reciprocal Rank Fusion (`reciprocal_rank_fusion`, pure/tested) merges the two ranks, then
  cluster-collapse (one story per cluster). Filters: vertical, period, ⭐-saved-only.

The **Ответить** button (`POST /search/answer`) asks the wiki first (`wiki_query.answer_from_wiki`); if the
wiki has nothing, it falls back to feed RAG (`core/ask.py`).

## Curated export (`GET /saved`)

⭐/👍 are the only hand-made layer in the system, so they get a first-class read API
(`NewsRepo.saved_items`) for anything built on top: export, newsletter, a page of one's own.
`/saved.rss` + `/saved.json` render the same set for readers.

- Feedback is aggregated **before** the join (`max(created_at)`, `array_agg(distinct signal)`): one item can
  carry ⭐ *and* 👍 (plus topic mutes), and a straight join would multiply it by the number of feedback rows.
- **No `processed_at`/`skip_reason` filter** — deliberate. A star is an explicit human decision and outranks
  any pipeline gate; whatever was marked is returned.
- `since` filters by **mark time**, not publication: the response's `latest_saved_at` is a watermark a client
  passes back for incremental sync. It is parsed leniently — `isoformat()` contains `+`, which a query string
  decodes as a space, so an un-encoded watermark would otherwise fail to make the round trip.

## ⭐ channel (`homyak-starchan` service, `core/starcard.py`)

Everything starred goes to its own Telegram channel as a Russian card, plus a daily top-5 digest.
Consumes `homyak.feedback.recorded` (durable `starchan`, `signal=save`, `action=added`) — so stars from
the bot *and* from `/lenta` both land, since both publish the same event. A separate process because a
card costs an LLM call (and sometimes a network fetch), which has no business inside the bot's push loop.
`news_items.star_posted_at` makes it idempotent; un-starring does not delete the post (owner's call).

Faithfulness is the whole point of the channel, and it is enforced structurally rather than by prompt:

- **No text, no retelling.** A third of stars arrive from lobsters/hn/github with 0–130 chars stored. The
  service re-runs `fetch_article` at publish time (the ingest-time attempt may have failed), and if there
  is still nothing, the card ships bare — title, link, tags. A thin card beats an invented one.
- **Three modes by source size** — `full` (through-line + 2-3 points), `brief` (one line from a short
  description, essentially a translation), `bare`.
- **Grounding check** (`ungrounded`, pure/tested, no second LLM call): every checkable token of the
  summary — Latin proper noun or number — must occur in the source. Offending phrases are dropped one by
  one; if the through-line itself fails, the card degrades to bare. Cyrillic is deliberately not checked
  (a retelling must reword), and the normalizer folds Unicode typography — a non-breaking hyphen in
  `GPT‑5.5` and a thin space in `1 290` both used to read as fabrications.
- **Prompt tested on real stars**, not invented samples: `homyak-starcard-eval [N] [--judge]` builds cards
  from actual ⭐ items and an independent LLM judge lists claims the source doesn't support — the only way
  to catch *relational* errors (mistranslated terms, shifted dates, dropped "only/not" qualifiers) that a
  token check cannot see. The judge stays in the eval; production pays for one call per card.

Digest: `STAR_DIGEST_HOURS` (local, default `10,23`) → top-5 of the last 24h from the whole IT feed, not
just stars — the channel has to have a pulse on days with zero stars. Slots are tracked by calendar date,
so a restart cannot double-post and a slot missed during downtime still goes out that day.

## LLM Wiki (`homyak-wiki` service, `core/wiki*.py`)

Adaptation of Karpathy's "LLM Wiki" (Apr 2026): **not RAG** — instead of retrieving raw fragments per query,
an LLM compiles saved items into a compounding, interlinked markdown base. Scoped to the user's curation
(⭐ save + 👍 like) rather than the firehose, so it stays small and dense.

- **Storage** — markdown files in the `./wiki` volume (Obsidian-viewable): `concepts/`, `entities/`,
  `sources/`, `index.md`, `log.md`, `lint.md`. `slugify` is pure/tested.
- **Ingest** (`wiki_ingest.ingest_item`) — the service consumes `homyak.feedback.recorded` (durable `wiki`,
  `signal in {up,save}`, `action=added`): writes a source page, an LLM extracts concepts/entities with a
  one-line takeaway, each merged into its page as a dated bullet with a `[[source]]` wikilink (idempotent by
  source-ref). Best-effort: an LLM failure still writes the source page + log.
- **Query** (`wiki_query.answer_from_wiki`) — reads the *synthesized* concept/entity/source pages most
  relevant to the question (keyword overlap; no embeddings needed at wiki scale) → LLM answer with citations.
- **Lint** (`wiki.run_lint`, periodic `WIKI_LINT_EVERY_HOURS`) — deterministic audit of weakly-linked pages
  → `lint.md`.
- **Backfill** — `homyak-wiki-backfill` builds the wiki from the full ⭐/👍 history in the `feedback` table
  (the service alone only sees new/retained feedback).

---

## Failure modes

| Scenario | Behavior |
|---|---|
| NATS down | Ingest keeps writing to PG, publish logs an error; the sweeper re-publishes when NATS returns. |
| Postgres down | Ingest fails; tscrapper keeps forwarding independently; telegram-ingest catches up. |
| Ollama down | Circuit breaker → `nak`; item retried later via JetStream. |
| Consumer zombie | JetStream `ack_wait` redelivers to another instance. |
| Duplicate on ingest | `ON CONFLICT DO UPDATE`, publish only when `was_new=true`. |
| Qdrant out of sync with PG | `homyak-reembed` re-embeds `WHERE embedding_version < current`. |
| Wiki LLM down on ingest | best-effort — source page + log still written; concept/entity extraction skipped. |
| Wiki behind on history | `homyak-wiki-backfill` replays all ⭐/👍 from the `feedback` table (idempotent). |
| Twitter bridge silent ≥6h | bot sends a one-off alert — `TWITTER_AUTH_TOKEN` likely expired (x.com via proxy, cookie in `.env`). |

---

## Key decisions

- **Wiki is not RAG** — an LLM compiles ⭐/👍 into a compounding markdown base; the human curates, the LLM
  keeps the bookkeeping. Firehose + hybrid search = discovery; the ⭐-wiki = the distilled second brain.
- **Hybrid search fused by RRF** — lexical (FTS) catches exact terms/names, semantic (Qdrant) catches meaning;
  RRF needs no score calibration between the two.

- **uv, not poetry** — speed, single binary, modern resolver. PEP 621 `pyproject.toml`, `uv.lock` in git.
- **NATS JetStream, not Kafka/Redis** — minimal infra, single binary, has durability/ack/replay.
- **Postgres + Qdrant (not pgvector)** — room to grow + Qdrant payload filters.
- **`(source_type, source_id)` UNIQUE, not URL** — the same URL from several sources becomes separate items
  merged into one `cluster_id`.
- **Event-driven via JetStream, not polling `processed_at IS NULL`** — ~ms latency, fan-out to many consumers.
- **tscrapper publishes to NATS** — durability decoupling; tscrapper is independent of Homyak's PG.
- **Verticals are independent** — a like in one vertical never leaks into another.
- **Expensive LLM judge cached, light components live** — learning affects ranking without reprocessing.
