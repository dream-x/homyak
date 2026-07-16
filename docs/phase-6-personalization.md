# Phase 6 — Персонализация: гибридный ранкер под интересы пользователя

> Флагманская фича Homyak. Всё остальное (источники, дедуп, лента) — инфраструктура ради этого.
> Превращает общую ленту в **персональный поток**: LLM-судья оценивает каждую новость против
> твоего профиля интересов, система дообучается на 👍/👎 прямо из Telegram-бота, самое релевантное
> пушится в личку, остальное — в дайджест по запросу.

## Цель

Заменить наивный `scorer = freshness * source_weight * cluster_size` (из `architecture.md`)
на **гибридный персональный скоринг**:

```
personal_score = w_llm    · llm_relevance          (LLM-судья против профиля, 0..1)
               + w_taste  · cosine(item, taste)    (близость к «вектору вкуса» — центроиду лайков)
               + w_tags   · tag_affinity(tags)     (обучаемые веса тегов, EMA от 👍/👎)
               + w_source · source_affinity         (обучаемые веса каналов/фидов)
               + w_fresh  · freshness_decay          (свежесть, экспоненциальный спад)
               − hard_mute (замьюченные темы → в фид вообще не попадают)
```

**Успех** = после недели использования лента в Telegram состоит преимущественно из новостей,
которые ты помечаешь 👍; поток нерелевантного (👎) падает от недели к неделе; на каждый пуш
видно **почему** он выбран; профиль можно править словами через бота, и система это учитывает.

## Prerequisites

- **Phase 1-2** — скелет + feedthrough (PG, NATS, ingest-poll, processor, API).
- **Phase 3** — эмбеддинги (bge-m3 + Qdrant): нужны для `taste_vector` и `cosine`.
- **Phase 4** — LLM (Qwen 2.5-14B) + `llm_tagger` + `llm_summarizer` + базовый TG-бот (push).
  Персонализация **расширяет** бот из Phase 4 (кнопки реакций, команды), а не создаёт новый.
- Источники активны: Telegram-relay, Miniflux, RSS.

> Phase 5 (Twitter + Web UI) **не требуется** — персонализация встаёт сразу после Phase 4.

---

## Ключевые концепции

### 1. Профиль интересов (`profile`)

Двухслойный: **свободный текст** (для LLM-судьи) + **структурированные темы** (для фильтров и cold-start весов).

```jsonc
{
  "version": 3,
  "description": "AI-агенты и агентные фреймворки, LLM inference и локальный self-hosting, Rust и системное программирование, ML-research (arXiv cs.CL/LG), инди-стартапы и dev-tools. НЕ интересно: крипта, политика, спорт, гаджет-обзоры.",
  "topics": [
    {"name": "ai-agents",     "polarity": "love", "weight": 1.0},
    {"name": "llm-inference", "polarity": "love", "weight": 1.0},
    {"name": "rust",          "polarity": "like", "weight": 0.7},
    {"name": "self-hosting",  "polarity": "like", "weight": 0.7},
    {"name": "crypto",        "polarity": "mute", "weight": -1.0},
    {"name": "politics",      "polarity": "mute", "weight": -1.0}
  ]
}
```

- `polarity`: `love | like | meh | dislike | mute`. `mute` → **жёсткий фильтр** (в фид не попадает).
- `description` — главный вход LLM-судьи. `topics` — seed для `tag_affinity` на cold-start.
- Профиль **версионируется**: каждая правка = новая версия. `news_items.scored_profile_version`
  фиксирует, против какой версии считался `llm_relevance` (для инвалидации кэша при смене профиля).

### 2. Вектор вкуса (`taste_vector`)

Инкрементальный центроид эмбеддингов **понравившихся** items (bge-m3, 1024-dim). Живёт в Qdrant
(коллекция `taste`, одна точка `id=1`) + метаданные в PG. На каждый 👍 — обновление среднего:

```
taste_new = taste_old + (item_embed − taste_old) / (n_liked + 1)   # incremental mean
n_liked  += 1
```

На 👎 — лёгкий отталкивающий сдвиг (опционально, малый lr, чтобы не разрушать центроид):
`taste_new = taste_old − lr_neg · (item_embed − taste_old)`.

**Cold-start:** пока `n_liked < TASTE_RAMP` (напр. 20), вес `w_taste` линейно растёт от 0 до полного —
на старте вкусовой вектор ненадёжен, доверяем LLM-судье и профилю.

### 3. Обучаемые аффинити (`tag_affinity`, `source_affinity`)

EMA-веса в `[-1..1]`:
- `tag_affinity[tag]` — по тегам из `llm_tagger`. 👍 двигает теги item'а вверх, 👎 — вниз.
- `source_affinity[(source_type, author)]` — по каналу/фиду/автору. Учит «HN тебе заходит, а techcrunch — нет».

Обновление (learning rate `FEEDBACK_LR`, напр. 0.1):
```
signal = +1 (👍) | −1 (👎)
w_new  = clip(w_old + lr · (signal − w_old), −1, 1)   # EMA к ±1
```

Cold-start `tag_affinity` — из `profile.topics` (love→+0.8, like→+0.5, dislike→−0.5, mute→−1).

### 4. LLM-судья (`llm_relevance`)

Qwen 2.5-14B через Ollama. Вход: `profile.description` + `topics` + (title, text[:1500], tags, summary).
Выход — **структурированный JSON**: `score` (0..1), `reason` (одна строка), `matched` (список интересов).
Кэшируется по `(item_id, profile_version)` — переоценка только при смене профиля или новом item'е.

Промпт (набросок, финал — в `homyak/adapters/analyzers/llm_relevance.py`):
```
Ты — персональный фильтр новостей. Профиль читателя:
«{description}»
Явные интересы: {love/like темы}. Не интересно: {dislike/mute}.

Новость:
Заголовок: {title}
Текст: {text[:1500]}
Теги: {tags}

Оцени релевантность читателю строго по профилю. Верни JSON:
{"score": 0..1, "reason": "<кратко почему>", "matched": ["<интерес>", ...]}
score=1 — точно в интересах; 0 — совсем мимо; учитывай явные «не интересно».
```

### 5. Гибридная свёртка (`personalizer`)

Финальная стадия pipeline. Веса — из конфига (тюнятся). **Что кэшируется, что считается на лету:**
- `llm_relevance` + `reason` — **дорого**, кэшируем в `news_items` при обработке.
- `taste / tags / source / fresh` — **дёшево** (vector + SQL), пересчитываются в feed/digest на лету
  по текущим весам. → Обучение сразу влияет на ранжирование без переобработки старых items.

`personal_score` при **ingestion** считается для **push-решения** (нужен снапшот). В feed/digest —
пересчёт лёгких компонент по актуальным весам, `llm_relevance` берётся из кэша.

### 6. Политика пуша (анти-спам)

Бот **не шлёт всё подряд**. Пуш только если:
- `personal_score >= PUSH_THRESHOLD` (напр. 0.7), **и**
- не превышен `MAX_PUSH_PER_HOUR` (напр. 8), **и**
- не «тихие часы» (`QUIET_HOURS`, напр. 0–8), **и**
- item — representative кластера (дубликаты не шлём).

Остальное копится и доступно через `/digest` (топ-N с прошлого запроса). При превышении rate-limit
в час — лучшие по score идут в пуш, хвост в дайджест.

---

## Модель данных (миграция 0004)

```sql
-- Профиль (версионируемый; активна одна строка)
CREATE TABLE profile (
    id            bigserial PRIMARY KEY,
    version       int NOT NULL,
    description   text NOT NULL,
    topics        jsonb NOT NULL DEFAULT '[]',
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_profile_active ON profile (active) WHERE active;

-- Обучаемые веса тегов
CREATE TABLE tag_affinity (
    tag        text PRIMARY KEY,
    weight     double precision NOT NULL DEFAULT 0,   -- [-1..1]
    n_pos      int NOT NULL DEFAULT 0,
    n_neg      int NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Обучаемые веса источников/каналов/авторов
CREATE TABLE source_affinity (
    source_type text NOT NULL,
    author      text NOT NULL DEFAULT '',
    weight      double precision NOT NULL DEFAULT 0,  -- [-1..1]
    n_pos       int NOT NULL DEFAULT 0,
    n_neg       int NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_type, author)
);

-- Сырой фидбек (источник обучения + аудита)
CREATE TABLE feedback (
    id           bigserial PRIMARY KEY,
    news_item_id bigint NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    signal       text NOT NULL,     -- up | down | save | mute_topic | open | skip
    topic        text,              -- для mute_topic
    surface      text NOT NULL DEFAULT 'tgbot',
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (news_item_id, signal)   -- один сигнал на item не дублируем (повторный клик = toggle)
);
CREATE INDEX idx_feedback_created ON feedback (created_at DESC);

-- Синглтон-метаданные вектора вкуса (сам вектор — в Qdrant collection "taste", point id=1)
CREATE TABLE taste_state (
    id         int PRIMARY KEY DEFAULT 1,
    n_liked    int NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (id = 1)
);

-- Персональный скоринг на news_items
ALTER TABLE news_items ADD COLUMN llm_relevance         double precision;
ALTER TABLE news_items ADD COLUMN llm_reason            text;
ALTER TABLE news_items ADD COLUMN personal_score        double precision;
ALTER TABLE news_items ADD COLUMN scored_profile_version int;
ALTER TABLE news_items ADD COLUMN pushed_at             timestamptz;

CREATE INDEX idx_news_personal ON news_items (personal_score DESC NULLS LAST);
CREATE INDEX idx_news_pushable  ON news_items (personal_score DESC)
    WHERE pushed_at IS NULL AND processed_at IS NOT NULL;
```

---

## Компоненты

### 1. LLM-судья — `homyak/adapters/analyzers/llm_relevance.py`

- `stage = 7` (после `llm_summarizer` из Phase 4, до `personalizer`).
- Читает активный профиль (кэш в памяти процессора, инвалидация по `profile.version`).
- Кэш: если `news_items.llm_relevance IS NOT NULL AND scored_profile_version == active.version` → skip.
- Ollama `POST /api/chat` (Qwen), `format: "json"`, temp≈0. Парсит `{score, reason, matched}`.
- Пишет `ctx.llm_relevance`, `ctx.llm_reason`; в PG — `llm_relevance`, `llm_reason`, `scored_profile_version`.
- Circuit breaker (из Phase 3) переиспользуется — при недоступности Ollama `nak` + retry.

### 2. Персонализатор — `homyak/adapters/analyzers/personalizer.py`

- `stage = 8` (финальная).
- Тянет `ctx.embedding` (от embedder), `tag_affinity` для `item.tags`, `source_affinity`,
  `taste` из Qdrant, применяет **hard-mute** (если tag ∈ muted topics → `personal_score = NULL`, вон из фида).
- Считает `personal_score` по формуле, пишет в PG.
- Решение о пуше **не здесь** — публикует `homyak.items.processed`, `tgbot-push` сам применяет политику.

### 3. Обучатель — `homyak/pipeline/learner.py` (новый процесс)

- Durable consumer на новом subject `homyak.feedback.recorded`.
- Payload: `{news_item_id, signal, topic?}`.
- Обработка по сигналу:
  - `up`   → taste centroid update (+), `tag_affinity[tags] +`, `source_affinity +`.
  - `down` → taste push-away (−, малый lr), `tag_affinity[tags] −`, `source_affinity −`.
  - `save` → как `up`, но с бóльшим весом (сильный позитив).
  - `mute_topic` → тег в таблицу `muted_tags` (слой «выученное»). В профиль НЕ пишем: кнопка не
    должна переписывать декларацию пользователя — так `medical: mute` выключил 21% вертикали.
    Снять: `homyak-interests unmute <вертикаль> <тег>`.
  - `open`/`skip` → слабые сигналы (опц., Phase 6.5): open как микро-плюс, skip как микро-минус.
- Идемпотентность: `feedback.UNIQUE(news_item_id, signal)` — повторный клик не даёт двойного обучения
  (toggle: повторный 👍 снимает лайк и откатывает вклад).
- Триггер **profile refinement** (см. ниже) — раз в N новых фидбеков или по расписанию.

### 4. Расширение TG-бота — `homyak/adapters/outputs/tg_bot.py`

Расширяет push-бота из Phase 4.

**Формат пуш-сообщения:**
```
🎯 92%  ·  AI-агенты, LLM
━━━━━━━━━━━━━━━━━━━━
<b>{title}</b>
{summary — 2-3 предложения из llm_summarizer}

🔗 {source_type}/{author}  ·  {relative_time}
💡 {llm_reason}
```
Inline-кнопки: `👍  👎  ⭐  🔇 тема  🔗 открыть`.

**Callback-хендлеры** → пишут в `feedback`, публикуют `homyak.feedback.recorded`, обновляют кнопку (✓).

**Команды:**
| Команда | Действие |
|---|---|
| `/digest [N]` | Топ-N непушенных с прошлого запроса, по `personal_score` |
| `/profile` | Показать текущий профиль (description + topics) |
| `/interest <текст>` | Добавить/уточнить интерес (→ новая версия профиля, LLM интегрирует в description) |
| `/mute <тема>` | Замьютить тему |
| `/why <id>` | Показать разбор скоринга item'а (компоненты формулы + llm_reason) |
| `/stats` | 👍/👎 за неделю, топ-теги, точность (доля 👍 среди пушей) |
| `/threshold <0..1>` | Подкрутить порог пуша |
| `/pause [часы]` | Пауза пушей |

### 5. Profile refinement (human-in-the-loop) — в `learner.py`

Раз в N фидбеков LLM смотрит последние 👍/👎 и предлагает **правку профиля** (не применяет молча):
```
Ты — куратор профиля интересов. Текущий профиль: «{description}».
За последнее время читатель лайкал: {liked titles+tags}. Дизлайкал: {disliked}.
Предложи уточнённый профиль (1 абзац) и список тем-кандидатов на love/mute. JSON.
```
Бот шлёт: *«Заметил: тебе заходят посты про X, мимо — про Y. Обновить профиль? [✅ Да] [✏️ Правки] [❌ Нет]»*.
Подтверждение → новая версия `profile` → инвалидация `llm_relevance`-кэша (переоценка свежих items).
Защита от дрейфа: только с явного согласия.

### 6. Пересчёт при смене профиля/весов

- **Смена профиля** (новая версия) → `llm_relevance` устаревает. Стратегия: **ленивая** переоценка —
  sweeper (из Phase 2) добирает `WHERE scored_profile_version < active.version AND published_at > now()-3d`
  и репаблишит `items.ingested` → processor переоценивает только свежее (старое не трогаем).
- **Смена аффинити-весов** (обучение) → `llm_relevance` не трогаем; лёгкие компоненты пересчитываются
  на лету в feed/digest. Ничего репаблишить не нужно.

---

## Конфигурация

`.env` (добавка к Phase 4):
```env
# Веса свёртки (сумма ≈ 1.0, тюнятся)
PERSONALIZE_LLM_WEIGHT=0.50
PERSONALIZE_TASTE_WEIGHT=0.20
PERSONALIZE_TAG_WEIGHT=0.15
PERSONALIZE_SOURCE_WEIGHT=0.10
PERSONALIZE_FRESH_WEIGHT=0.05
# Обучение
FEEDBACK_LR=0.10            # learning rate для tag/source EMA
TASTE_NEG_LR=0.03          # слабый отталкивающий сдвиг на 👎
TASTE_RAMP=20              # лайков до полного веса taste
# Политика пуша
PUSH_THRESHOLD=0.70
MAX_PUSH_PER_HOUR=8
QUIET_HOURS=0-8            # локальное время
PROFILE_REFINE_EVERY=25    # фидбеков между предложениями правки профиля
```

`config/sources.yaml` — без изменений (веса источников теперь обучаемые, `weight` там → только seed).

---

## Изменения в существующем коде

| Файл | Изменение |
|---|---|
| `core/interfaces.py` | `AnalyzerContext` += `llm_relevance`, `llm_reason`, `personal_score` |
| `core/models.py` | ORM для `profile`, `tag_affinity`, `source_affinity`, `feedback`, `taste_state`; новые колонки `news_items` |
| `core/events.py` | `publish_feedback()`, `consume_feedback()` на `homyak.feedback.recorded` |
| `storage/postgres.py` | `record_feedback`, `get_active_profile`, `bump_tag/source_affinity`, `get_affinities`, `feed()` учитывает `personal_score` и hard-mute |
| `storage/qdrant.py` | коллекция `taste` (dim=1024), `update_taste`, `get_taste` |
| `pipeline/processor.py` | подключить `llm_relevance` (stage 7) + `personalizer` (stage 8) |
| `adapters/outputs/tg_bot.py` | кнопки реакций, callback-хендлеры, команды |
| `pipeline/sweeper.py` | добор устаревших по `scored_profile_version` для ленивой переоценки |
| `pyproject.toml` | entry point `homyak-learner = "homyak.pipeline.learner:main"` |

Новые файлы: `analyzers/llm_relevance.py`, `analyzers/personalizer.py`, `pipeline/learner.py`,
`core/scoring.py` (чистая функция свёртки — легко тестируется).

---

## Тесты

- `test_scoring.py` — чистая свёртка `personal_score`: cold-start ramp, hard-mute, границы.
- `test_affinity_update.py` — EMA tag/source: сходимость к ±1, clip, toggle-откат.
- `test_taste_update.py` — инкрементальный центроид корректен (сравнение с batch-mean).
- `test_llm_relevance.py` — мок Ollama, парсинг JSON, кэш по `profile_version`.
- `test_hard_mute.py` — item с mute-тегом не попадает в feed.
- `test_feedback_idempotent.py` — повторный сигнал не даёт двойного обучения.
- `test_push_policy.py` — threshold, rate-limit, quiet hours, только representatives.
- `test_bot_callbacks.py` — клик 👍 → строка в feedback + publish.
- `test_profile_versioning.py` — правка профиля → инвалидация кэша llm_relevance.

---

## Acceptance criteria

```bash
cd /Users/maks/projects/homyak
docker compose up -d postgres nats qdrant ollama

# 1. Миграция
uv run alembic upgrade head            # применит 0004

# 2. Стартовый профиль
uv run homyak-interests apply   # из config/interests.yaml; или через бот /profile

# 3. Процессы
uv run homyak-ingest-poll &
uv run homyak-tg-relay &
uv run homyak-processor &              # теперь со stage'ами llm_relevance + personalizer
uv run homyak-learner &                # новый: учится на фидбеке
uv run homyak-tgbot                    # бот с кнопками

# 4. Проверки
# Персональный скоринг проставлен
psql ... -c "SELECT id, round(personal_score::numeric,2), llm_reason FROM news_items
             WHERE personal_score IS NOT NULL ORDER BY personal_score DESC LIMIT 10"

# Hard-mute работает
psql ... -c "SELECT count(*) FROM news_items WHERE 'crypto' = ANY(tags) AND personal_score IS NOT NULL"
# → 0 (замьючено)

# Бот прислал в личку только высокорелевантное
# Жмём 👍 на посте про AI-агентов → в БД:
psql ... -c "SELECT signal, count(*) FROM feedback GROUP BY 1"
# up растёт

# Обучение сработало
psql ... -c "SELECT tag, round(weight::numeric,2) FROM tag_affinity ORDER BY weight DESC LIMIT 10"
# теги лайкнутого поднялись; taste_state.n_liked > 0

# Объяснимость
# В боте /why <id> → показывает разбивку: llm 0.9·0.5 + taste 0.8·0.2 + tags ... = 0.87
```

**Критерий приёмки фичи (через ~неделю реального использования):**
- Доля 👍 среди пушей (precision) заметно выше, чем среди случайной выборки ленты.
- `/stats` показывает рост precision от недели к неделе.
- Замьюченные темы не появляются.
- Каждый пуш объясним (`/why`).

---

## Checklist

- [ ] Миграция 0004: `profile`, `tag_affinity`, `source_affinity`, `feedback`, `taste_state`, колонки `news_items`
- [ ] `core/scoring.py` — чистая свёртка, покрыта тестами (cold-start, mute, границы)
- [ ] `analyzers/llm_relevance.py` — LLM-судья, JSON-выход, кэш по `profile_version`
- [ ] `analyzers/personalizer.py` — гибридная свёртка, hard-mute
- [ ] Qdrant-коллекция `taste`, инкрементальный центроид корректен
- [ ] `pipeline/learner.py` — consumer фидбека, EMA-обновления, toggle-идемпотентность
- [ ] TG-бот: кнопки 👍👎⭐🔇🔗, callback → feedback + publish
- [ ] TG-бот команды: `/digest /profile /interest /mute /why /stats /threshold /pause`
- [ ] Политика пуша: threshold + rate-limit + quiet hours + только representatives
- [ ] Profile refinement: предложение правки с подтверждением, версионирование, инвалидация кэша
- [ ] Ленивая переоценка через sweeper при смене профиля
- [ ] Все тесты из секции зелёные
- [ ] `alembic downgrade base && upgrade head` — повторяемо

---

## Key decisions

- **Гибрид, не чистый ML** — LLM-судья даёт объяснимость и мгновенный cold-start (профиль словами),
  обучение на фидбеке добавляет адаптацию. Чистый collaborative filtering невозможен (один юзер),
  чистый keyword-фильтр слишком туп.
- **LLM-relevance кэшируется, аффинити — на лету** — дорогой сигнал считаем раз, дешёвые
  пересчитываем при каждом чтении → обучение влияет мгновенно без переобработки.
- **Push-политика отдельно от скоринга** — `personalizer` только оценивает, `tgbot-push` решает
  слать ли. Позволяет менять пороги без переобработки.
- **Human-in-the-loop для профиля** — авто-дрейф профиля опасен (можно скатиться в эхо-камеру/шум),
  правки только с подтверждения. Веса аффинити двигаются автоматически (они мягкие и обратимые).
- **Toggle-фидбек** — повторный клик снимает реакцию и откатывает обучение; `UNIQUE(item, signal)`.
- **Hard-mute = фильтр, не вес** — замьюченное не должно протекать даже при высоком llm_score.
- **taste в Qdrant, не в PG** — переиспользуем векторную инфру, cosine нативно.
- **Профиль версионируется** — инвалидация кэша llm_relevance завязана на `scored_profile_version`.

---

## Failure modes

| Сценарий | Поведение |
|---|---|
| Ollama упал | `llm_relevance` не считается → circuit breaker → `nak`/retry (как Phase 3). `personal_score` ждёт. |
| Профиль пуст (cold start) | `w_taste=0`, вес на LLM-судью + seed tag_affinity из `topics`. Работает с первого дня. |
| Мало лайков | `taste` ramp держит `w_taste` низким, пока `n_liked < TASTE_RAMP`. Нет мусорного центроида. |
| Обучение «сломалось» (перекос) | Веса в `[-1..1]` + EMA обратимы; `/profile` показывает, ручной reset аффинити. |
| Спам пушей | `MAX_PUSH_PER_HOUR` + `PUSH_THRESHOLD` + quiet hours. Хвост → `/digest`. |
| Смена профиля | Ленивая переоценка только свежих (3д) через sweeper; старое не трогаем. |
| Двойной клик реакции | `UNIQUE(item, signal)` + toggle — обучение не задваивается. |

---

## Phasing внутри фичи (порядок реализации)

1. **6.0 — Скоринг-ядро (offline):** миграция 0004, `scoring.py`, `personalizer` + `llm_relevance`,
   персональный `personal_score` в БД. Проверка: топ ленты по `personal_score` в `psql`/API. Без бота.
2. **6.1 — Бот-реакции + обучение:** кнопки в TG-боте, `feedback`, `learner`, EMA + taste. Проверка:
   лайки двигают веса, лента переранжируется.
3. **6.2 — Политика пуша + команды:** threshold/rate-limit/quiet, `/digest /why /stats /profile /mute`.
4. **6.3 — Profile refinement:** предложения правок с подтверждением, ленивая переоценка.

> Начинаем с **6.0** — оно не требует бота и проверяется в `psql`, даёт быстрый сигнал, что скоринг осмысленный.
