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
