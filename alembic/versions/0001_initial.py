"""initial schema: news_items, clusters, ingest_state

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # clusters — без FK representative_id (news_items ещё нет); FK добавим в конце.
    op.create_table(
        "clusters",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("representative_id", sa.BigInteger(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "news_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("text", sa.Text()),
        sa.Column(
            "media",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("author", sa.String(255)),
        sa.Column("raw_score", sa.Float()),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("category", sa.String(64)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("cluster_id", sa.BigInteger()),
        sa.Column("embedding_model", sa.String(64)),
        sa.Column("embedding_version", sa.Integer()),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("processing_started_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("retry_after", sa.DateTime(timezone=True)),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(text,''))",
                persisted=True,
            ),
        ),
        sa.UniqueConstraint("source_type", "source_id", name="uq_news_source"),
        sa.ForeignKeyConstraint(
            ["cluster_id"], ["clusters.id"], ondelete="SET NULL", name="fk_news_cluster"
        ),
    )

    op.create_table(
        "ingest_state",
        sa.Column("source_name", sa.String(255), primary_key=True),
        sa.Column("cursor", sa.Text()),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
    )

    op.create_index(
        "idx_news_unprocessed",
        "news_items",
        ["id"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )
    op.create_index("idx_news_published", "news_items", [sa.text("published_at DESC")])
    op.create_index("idx_news_cluster", "news_items", ["cluster_id"])
    op.create_index("idx_news_fts", "news_items", ["search_tsv"], postgresql_using="gin")

    # Циклический FK — после создания обеих таблиц.
    op.create_foreign_key(
        "fk_cluster_repr",
        "clusters",
        "news_items",
        ["representative_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_cluster_repr", "clusters", type_="foreignkey")
    op.drop_index("idx_news_fts", table_name="news_items")
    op.drop_index("idx_news_cluster", table_name="news_items")
    op.drop_index("idx_news_published", table_name="news_items")
    op.drop_index("idx_news_unprocessed", table_name="news_items")
    op.drop_table("ingest_state")
    op.drop_table("news_items")
    op.drop_table("clusters")
