"""Настройки приложения (.env) + декларативный конфиг источников (config/sources.yaml).

Секреты (токены) — только из env; в YAML указываем имя env-переменной через `token_env`.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    nats_url: str = Field(default="nats://localhost:4222", alias="NATS_URL")
    sources_config_path: str = Field(default="config/sources.yaml", alias="SOURCES_CONFIG_PATH")
    # «Что мне нравится» — ЕДИНСТВЕННОЕ место: вертикали, watch и веса свёртки.
    # Сюда же уехали PERSONALIZE_*_WEIGHT / TASTE_RAMP / PUSH_THRESHOLD / WATCHLIST_BOOST /
    # PREFILTER_MIN_SIM: интересы не должны задаваться в шести местах (см. core/interests.py).
    interests_path: str = Field(default="config/interests.yaml", alias="INTERESTS_PATH")

    # Окно ингеста = max(interval * factor, min_window). Курсор и так даёт «только новое»;
    # окно — лишь страховка от залпа истории при первом контакте и долгов после простоя.
    # ПОЛ ОБЯЗАТЕЛЕН: без него окно меряло не то время и молча убивало источники —
    # у hn pubDate это момент сабмита (на морду через 0.5-3ч), habr_best — дайджест за сутки,
    # huggingface/nature датируют 00:00. Окно в 15-90 мин не ловило их в принципе.
    ingest_age_factor: float = Field(default=1.5, alias="INGEST_AGE_FACTOR")
    ingest_min_window_hours: float = Field(default=24.0, alias="INGEST_MIN_WINDOW_HOURS")

    # Поллинг: не долбим источники (особенно RSSHub на одном twitter-токене).
    poll_concurrency: int = Field(default=3, alias="POLL_CONCURRENCY")  # макс. одновременных фетчей
    poll_stagger_seconds: float = Field(default=7.0, alias="POLL_STAGGER_SECONDS")  # разнос старта между источниками
    poll_jitter_seconds: int = Field(default=120, alias="POLL_JITTER_SECONDS")  # случайный сдвиг каждого прогона

    # Phase 3: эмбеддинги + similarity
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    # Два хоста: основной — GPU-бокс (5090), запасной — локальная Ollama на Metal.
    # У них РАЗНЫЕ модели, поэтому фолбэк идёт парой (url, model) — см. core/ollama.py.
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    ollama_url_fallback: str | None = Field(default=None, alias="OLLAMA_URL_FALLBACK")
    embedding_model: str = Field(default="bge-m3", alias="EMBEDDING_MODEL")
    embedding_version: int = Field(default=1, alias="EMBEDDING_VERSION")
    embedding_batch_size: int = Field(default=32, alias="EMBEDDING_BATCH_SIZE")
    similarity_threshold: float = Field(default=0.88, alias="SIMILARITY_THRESHOLD")

    # Извлечение статей: фолбэк на reader-сервис (r.jina.ai) при бот-блоке/JS. Внешний вызов.
    article_reader_fallback: bool = Field(default=True, alias="ARTICLE_READER_FALLBACK")
    # Хосты с жёсткой бот-стеной (403 даже reader'у) — не пытаемся тянуть, берём RSS-огрызок.
    # x.com/twitter.com — текст твита уже пришёл из RSSHub, а сам сайт всё равно 403'ит.
    article_skip_hosts: str = Field(
        default="investing.com,x.com,twitter.com", alias="ARTICLE_SKIP_HOSTS"
    )

    # Phase 4: LLM
    # qwen3.5:9b — новейший, 1.22с/айтем (быстрее старого qwen2.5:14b при 3.5x меньшем размере),
    # вертикали 3/4, и лучше всех отделяет мусор (SEC-филинг → insight 0.0). Требует think=False.
    llm_model: str = Field(default="qwen3.5:9b", alias="LLM_MODEL")
    llm_model_fallback: str | None = Field(default=None, alias="LLM_MODEL_FALLBACK")
    llm_num_ctx: int = Field(default=32768, alias="LLM_NUM_CTX")
    # Саммари — на той же qwen3.5:9b: облачная gpt-oss:120b упёрлась в недельный лимит (429),
    # каждое саммари молча падало на gemma4 — то есть 120B мы и так уже не получали, а в памяти
    # висели три модели (17.2G). Одна модель на теггер+судью+саммари = 2 модели в памяти (7.5G).
    summary_model: str = Field(default="qwen3.5:9b", alias="SUMMARY_MODEL")
    summary_fallback_model: str | None = Field(default=None, alias="SUMMARY_FALLBACK_MODEL")

    # Гейт предобработки: отсекает шум до дорогих LLM-стадий (теггер+судья).
    # Выключатель — тут, а порог (prefilter_min_sim) — в config/interests.yaml: это настройка
    # «что мне интересно», а не эксплуатации.
    prefilter_enabled: bool = Field(default=True, alias="PREFILTER_ENABLED")

    # Phase 6: обучение (слой 2). Веса свёртки personal_score — НЕ здесь, а в interests.yaml.
    feedback_lr: float = Field(default=0.10, alias="FEEDBACK_LR")
    # Twitter в пушах — отдельная история: свой (пониженный) порог + обход pushonly-скоупа.
    # Иначе tw-инсайды из business/medical (их большинство) не долетают, а порог вертикалей высок.
    twitter_push_threshold: float = Field(default=0.50, alias="TWITTER_PUSH_THRESHOLD")
    # Лента-канал: бот публикует общую ленту (ВСЕ вертикали, не завязан на личный pushonly)
    # в отдельный TG-канал. Пусто = выключено. Бот должен быть админом канала.
    # ID канала (-100…) или @username. Порог — свой, независимый от личного пуша.
    feed_channel_id: str = Field(default="", alias="FEED_CHANNEL_ID")
    # 0.60 ≈ 350 постов/сутки (~15/час). Ниже — гуще (0.50 ≈ 815), выше — реже (0.65 ≈ 183).
    channel_min_score: float = Field(default=0.60, alias="CHANNEL_MIN_SCORE")
    # Скоуп вертикалей канала (csv, напр. "it"). Пусто = все. IT-твиты сюда входят —
    # они тоже vertical=it. Безвертикальный шум не проходит в любом случае (нет score).
    channel_verticals: str = Field(default="", alias="CHANNEL_VERTICALS")
    taste_neg_lr: float = Field(default=0.03, alias="TASTE_NEG_LR")
    profile_refine_every: int = Field(default=10, alias="PROFILE_REFINE_EVERY")
    # LLM-вика (сервис homyak-wiki): каталог markdown-страниц, компаундится из ⭐/👍.
    # Смонтирован volume'ом → смотрится в Obsidian, рендерится на /search. lint_every_hours=0 = выкл.
    wiki_dir: str = Field(default="/app/wiki", alias="WIKI_DIR")
    wiki_lint_every_hours: int = Field(default=24, alias="WIKI_LINT_EVERY_HOURS")
    max_push_per_hour: int = Field(default=8, alias="MAX_PUSH_PER_HOUR")
    quiet_hours: str = Field(default="0-8", alias="QUIET_HOURS")  # локальные часы, "start-end"

    # Telegram-бот
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    # Кому МОЖНО писать боту (Telegram user id, через запятую). Бот приватный: без allowlist
    # любой, кто знает @username, читает твои ленты, портит обучение и /start'ом угоняет пуши
    # себе. Пусто → бот НИКОГО не пускает (fail-closed), а не всех.
    telegram_allowed_ids: str = Field(default="", alias="TELEGRAM_ALLOWED_IDS")
    # HTTP-прокси для Bot API (api.telegram.org). Нужен на хостах без прямого доступа к
    # Telegram (VM за роутером). Bot API проходит через обычный HTTP-прокси (в отличие от
    # MTProto-DC у tscrapper). Пусто = напрямую.
    telegram_bot_proxy: str = Field(default="", alias="TELEGRAM_BOT_PROXY")
    telegram_outbox_path: str = Field(
        default="/var/lib/tscrapper/outbox.jsonl", alias="TELEGRAM_OUTBOX_PATH"
    )


settings = Settings()


# --- декларация источников (YAML) ---


class MinifluxConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://localhost:8080"
    token_env: str = "MINIFLUX_TOKEN"
    interval_seconds: int = 300
    categories: list[str] | None = None

    @property
    def token(self) -> str | None:
        return os.getenv(self.token_env)


class RSSFeedConfig(BaseModel):
    name: str
    url: str
    category: str | None = None
    interval_seconds: int = 600
    weight: float = 1.0


class SourcesConfig(BaseModel):
    miniflux: MinifluxConfig | None = None
    rss: list[RSSFeedConfig] = Field(default_factory=list)


def load_sources_config(path: str | None = None) -> SourcesConfig:
    p = Path(path or settings.sources_config_path)
    if not p.exists():
        return SourcesConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return SourcesConfig.model_validate(data)
