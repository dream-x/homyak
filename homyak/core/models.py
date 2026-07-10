"""SQLAlchemy 2.x declarative — целевая схема БД Homyak.

Истина схемы — в миграциях (`alembic/versions/`); эти модели держим синхронными для типов и
будущих autogenerate-диффов.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    url_normalized: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    media: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )
    author: Mapped[str | None] = mapped_column(String(255))
    raw_score: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=sa_text("'{}'::text[]")
    )
    category: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    cluster_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("clusters.id", ondelete="SET NULL", name="fk_news_cluster")
    )
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    embedding_version: Mapped[int | None] = mapped_column(Integer)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(text,''))",
            persisted=True,
        ),
    )

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_news_source"),
        Index("idx_news_unprocessed", "id", postgresql_where=sa_text("processed_at IS NULL")),
        Index("idx_news_published", sa_text("published_at DESC")),
        Index("idx_news_cluster", "cluster_id"),
        Index("idx_news_fts", "search_tsv", postgresql_using="gin"),
        Index(
            "idx_news_url_normalized",
            "url_normalized",
            postgresql_where=sa_text("url_normalized IS NOT NULL"),
        ),
    )


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    representative_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("news_items.id", ondelete="SET NULL", use_alter=True, name="fk_cluster_repr"),
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IngestState(Base):
    __tablename__ = "ingest_state"

    source_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
