# Phase 5 — Twitter/X via RSSHub

## Goal

Bring Twitter/X accounts into the feed **with zero new adapter code** by treating each account as an RSS
feed served by a self-hosted **RSSHub** bridge. The existing `rss` source adapter ingests them; the tagger
classifies each tweet into a vertical (business/it/medical), the judge scores it — same pipeline as everything else.

## Why RSSHub (not the official API)

In 2026 free anonymous Twitter reading is effectively dead:
- Nitter — most instances shut down.
- X API v2 — reads are paid (Basic ~$100+/mo; filtered stream is Pro/Enterprise).
- **RSSHub** — self-hosted bridge, free, exposes `@account` as RSS. Needs a Twitter `auth_token` cookie and
  can be flaky, but requires **no code** in Homyak (reuses the RSS adapter).

## Setup

1. **RSSHub** — added to `docker-compose.yml` as service `rsshub` (image `diygod/rsshub`, port 1200).
   App services reach it at `http://rsshub:1200` on the compose network.
2. **Auth** — set `TWITTER_AUTH_TOKEN` in `.env` (the `auth_token` cookie from your logged-in X account:
   DevTools → Application → Cookies → `auth_token`; several comma-separated tokens rotate). Without it the
   Twitter routes return nothing.
3. **Accounts** — declared in `config/sources.yaml` as RSS feeds:
   ```yaml
   - {name: tw_karpathy, url: "http://rsshub:1200/twitter/user/karpathy", category: ai, interval_seconds: 1800, weight: 1.0}
   ```
   `feed_name` becomes `tw_<handle>`, so `source_affinity` learns per-account. Add/remove freely — `config/`
   is a mounted volume, no rebuild.

## Run

```bash
podman compose up -d rsshub           # bring up the bridge
#   put TWITTER_AUTH_TOKEN in .env
podman compose up -d --force-recreate ingest-poll   # pick up the twitter feeds
# a tweet flows: ingest-poll → NATS → processor (fetch/tags/vertical/judge/score) → /business|/it|/medical
```

## Notes

- **Reliability:** RSSHub's Twitter route breaks when X changes things or the cookie expires — rotate tokens,
  watch `podman logs homyak-rsshub-1`.
- **raw_score:** RSSHub RSS doesn't expose like/retweet counts cleanly, so tweets currently ride on the same
  base scoring as RSS. A dedicated `twitter.py` `PollSource` (official/third-party API) could map engagement
  → `raw_score` later — the interface is ready.
- **Web UI** (the other half of the original Phase 5) — not started; the REST/SSE API is the backend for it.
