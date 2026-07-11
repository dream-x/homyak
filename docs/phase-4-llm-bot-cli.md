# Phase 4 — LLM-обогащение + Telegram-бот + CLI

## Цель

Досыпать в pipeline **LLM-стадии** (теги, саммари, скоринг) на локальном `qwen2.5:14b` через Ollama
и добавить **выходы для чтения**: Telegram-бот (push) и CLI/TUI. Это последний слой перед флагманской
персонализацией (Phase 6): LLM-инфраструктура (`core/llm.py`) и TG-бот переиспользуются там для
LLM-судьи и кнопок-фидбека.

**Успех** = у обработанных items появляются `tags`, `summary`, `score`; лента сортируется по `score`;
бот шлёт топ-новости в Telegram; CLI показывает ленту в терминале.

## Prerequisites

- Phase 1-3 завершены (PG, NATS, Qdrant, embeddings, similarity). Ollama с `qwen2.5:14b`.
- Для бота — токен от @BotFather (секрет пользователя, в `.env` как `TELEGRAM_BOT_TOKEN`).

## Scope

- `core/llm.py` — обёртка Ollama `/api/chat` (JSON и text режимы) + circuit breaker (переиспользуем `core/circuit.py`).
- `core/scoring.py` — чистая функция `base_score(published_at, raw_score, cluster_size)` (freshness-decay).
- `adapters/analyzers/llm_tagger.py` (stage 4) — топ-5 тегов из словаря + свободные, JSON-выход.
- `adapters/analyzers/llm_summarizer.py` (stage 5) — 2-3 предложения на языке оригинала.
- `adapters/analyzers/scorer.py` (stage 6) — `base_score`, читает размер кластера.
- Миграция 0004 — `news_items.summary text`, `news_items.score double precision`, `idx_news_score`.
- `mark_processed` пишет `summary`/`score`; feed умеет сортировать по `score`.
- `adapters/outputs/tg_bot.py` — push top-новостей (aiogram/прямой Bot API). **Требует токен.**
- `adapters/outputs/cli.py` — Textual TUI / простой вывод ленты. Токен не нужен.

## Что НЕ в scope

- Персональный скоринг, профиль, обучение, кнопки-фидбек — это **Phase 6** (флагман).
- `scorer` здесь — базовый (freshness · cluster · raw), без персонализации.

---

## Стадии pipeline (после Phase 3)

```
1 url_dedup → 2 embedder → 3 similarity_dedup → 4 llm_tagger → 5 llm_summarizer → 6 scorer
```

LLM-стадии (4,5) — **best-effort**: при сбое парсинга не блокируют item (теги/саммари опциональны);
падение соединения с Ollama уже отлавливается на stage 2 (embedder) circuit breaker'ом → nak+retry.
`scorer` (6) считается всегда (не зависит от LLM).

### llm_tagger (stage 4)
- Ollama `/api/chat`, `format=json`, temp≈0.1. Промпт: словарь тем + title + text[:1500].
- Выход `{"tags": [...]}` → `ctx.tags` (до 5, только строки). Словарь-затравка в коде, свободные теги разрешены.

### llm_summarizer (stage 5)
- Ollama `/api/chat` (text), 2-3 предложения на языке оригинала. → `ctx.summary`.

### scorer (stage 6)
- `base_score = freshness · (1+raw_score) · (1+ln(cluster_size))`, `freshness = exp(-age_h/48)`.
- Размер кластера — из `clusters.size` по `ctx.cluster_id`. → `ctx.score`.

---

## Модель данных (миграция 0004)

```sql
ALTER TABLE news_items ADD COLUMN summary text;
ALTER TABLE news_items ADD COLUMN score   double precision;
CREATE INDEX idx_news_score ON news_items (score DESC NULLS LAST);
```

> Phase 6 (персонализация) добавит свои таблицы отдельной миграцией (0005+); её `personal_score`
> заменит `score` в ранжировании ленты.

---

## Выходы

### Telegram-бот (`adapters/outputs/tg_bot.py`) — требует токен
- Consumer на `homyak.items.processed`; шлёт в личку новости с `score >= порога`.
- Формат: заголовок, саммари, источник, ссылка. (Кнопки-реакции 👍/👎 — Phase 6.)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` из env.

### CLI/TUI (`adapters/outputs/cli.py`)
- Textual-лента или простой `rich`-вывод топ-N по `score`. Токен не нужен.

---

## Acceptance criteria

```bash
uv run alembic upgrade head          # 0004
uv run homyak-processor              # теперь с llm_tagger/summarizer/scorer
# после обработки:
psql ... -c "SELECT tags, left(summary,60), round(score::numeric,3) FROM news_items WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10"
# tags непустые, summary осмысленный, score убывает
uv run homyak-cli                    # лента в терминале
# при наличии токена:
uv run homyak-tgbot                  # бот шлёт топ-новости
```

## Checklist

- [ ] `core/llm.py` — chat JSON/text + circuit breaker
- [ ] `core/scoring.py` — `base_score`, покрыт тестами (freshness decay, cluster, границы)
- [ ] `llm_tagger` (stage 4) — JSON-теги, best-effort
- [ ] `llm_summarizer` (stage 5) — саммари, best-effort
- [ ] `scorer` (stage 6) — base_score, читает cluster size
- [ ] Миграция 0004 (summary, score, idx_news_score), ORM синхронизирован
- [ ] `mark_processed` пишет summary/score; feed сортирует по score (опц. параметр)
- [ ] E2E на живом qwen2.5:14b: item получает tags+summary+score
- [ ] CLI показывает ленту
- [ ] TG-бот (после токена) шлёт топ-новости
- [ ] `alembic downgrade -1 && upgrade head` повторяемо

## Key decisions

- **LLM best-effort на 4-5** — enrichment не должен ронять пайплайн; критичный отказ Ollama ловится на stage 2.
- **`score` отдельной колонкой, не в raw_score** — `raw_score` = внешняя оценка источника (Twitter), `score` = вычисленный.
- **core/llm.py переиспользуется в Phase 6** — там LLM-судья против профиля; та же обёртка + circuit breaker.
- **Бот отделён от токена** — анализаторы и CLI не требуют секретов; бот включается по наличию `TELEGRAM_BOT_TOKEN`.
