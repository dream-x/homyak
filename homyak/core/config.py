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

    # Phase 3: эмбеддинги + similarity
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    embedding_model: str = Field(default="bge-m3", alias="EMBEDDING_MODEL")
    embedding_version: int = Field(default=1, alias="EMBEDDING_VERSION")
    embedding_batch_size: int = Field(default=32, alias="EMBEDDING_BATCH_SIZE")
    similarity_threshold: float = Field(default=0.88, alias="SIMILARITY_THRESHOLD")

    # Phase 4: LLM
    llm_model: str = Field(default="qwen2.5:14b", alias="LLM_MODEL")
    summary_model: str = Field(default="gpt-oss:120b-cloud", alias="SUMMARY_MODEL")
    summary_fallback_model: str = Field(default="gemma4:latest", alias="SUMMARY_FALLBACK_MODEL")

    # Phase 6: персонализация (веса свёртки personal_score)
    personalize_llm_weight: float = Field(default=0.50, alias="PERSONALIZE_LLM_WEIGHT")
    personalize_taste_weight: float = Field(default=0.20, alias="PERSONALIZE_TASTE_WEIGHT")
    personalize_tag_weight: float = Field(default=0.15, alias="PERSONALIZE_TAG_WEIGHT")
    personalize_source_weight: float = Field(default=0.10, alias="PERSONALIZE_SOURCE_WEIGHT")
    personalize_fresh_weight: float = Field(default=0.05, alias="PERSONALIZE_FRESH_WEIGHT")
    taste_ramp: int = Field(default=20, alias="TASTE_RAMP")
    push_threshold: float = Field(default=0.55, alias="PUSH_THRESHOLD")  # cold-start: taste=0
    feedback_lr: float = Field(default=0.10, alias="FEEDBACK_LR")
    taste_neg_lr: float = Field(default=0.03, alias="TASTE_NEG_LR")
    profile_refine_every: int = Field(default=10, alias="PROFILE_REFINE_EVERY")
    max_push_per_hour: int = Field(default=8, alias="MAX_PUSH_PER_HOUR")
    quiet_hours: str = Field(default="0-8", alias="QUIET_HOURS")  # локальные часы, "start-end"

    # Telegram-бот
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
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
