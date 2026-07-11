"""персонализация: profile, tag/source affinity, feedback, taste + колонки скоринга

Revision ID: 0005_personalization
Revises: 0004_llm_summary_score
Create Date: 2026-07-11

Примечание: в docs/phase-6-personalization.md эта миграция названа 0004 — фактически 0005
(0004 занята summary/score из Phase 4).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_personalization"
down_revision = "0004_llm_summary_score"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profile",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("topics", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_profile_active", "profile", ["active"], unique=True, postgresql_where=sa.text("active"))

    op.create_table(
        "tag_affinity",
        sa.Column("tag", sa.Text(), primary_key=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="0"),
        sa.Column("n_pos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_neg", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "source_affinity",
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False, server_default=""),
        sa.Column("weight", sa.Float(), nullable=False, server_default="0"),
        sa.Column("n_pos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_neg", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("source_type", "author"),
    )

    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("news_item_id", sa.BigInteger(), nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text()),
        sa.Column("surface", sa.Text(), nullable=False, server_default="tgbot"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["news_item_id"], ["news_items.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("news_item_id", "signal", name="uq_feedback_item_signal"),
    )
    op.create_index("idx_feedback_created", "feedback", [sa.text("created_at DESC")])

    op.create_table(
        "taste_state",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("n_liked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_taste_singleton"),
    )

    op.add_column("news_items", sa.Column("llm_relevance", sa.Float()))
    op.add_column("news_items", sa.Column("llm_reason", sa.Text()))
    op.add_column("news_items", sa.Column("personal_score", sa.Float()))
    op.add_column("news_items", sa.Column("scored_profile_version", sa.Integer()))
    op.add_column("news_items", sa.Column("pushed_at", sa.DateTime(timezone=True)))
    op.create_index("idx_news_personal", "news_items", [sa.text("personal_score DESC NULLS LAST")])
    op.create_index(
        "idx_news_pushable",
        "news_items",
        [sa.text("personal_score DESC")],
        postgresql_where=sa.text("pushed_at IS NULL AND processed_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_news_pushable", table_name="news_items")
    op.drop_index("idx_news_personal", table_name="news_items")
    op.drop_column("news_items", "pushed_at")
    op.drop_column("news_items", "scored_profile_version")
    op.drop_column("news_items", "personal_score")
    op.drop_column("news_items", "llm_reason")
    op.drop_column("news_items", "llm_relevance")
    op.drop_table("taste_state")
    op.drop_index("idx_feedback_created", table_name="feedback")
    op.drop_table("feedback")
    op.drop_table("source_affinity")
    op.drop_table("tag_affinity")
    op.drop_index("idx_profile_active", table_name="profile")
    op.drop_table("profile")
