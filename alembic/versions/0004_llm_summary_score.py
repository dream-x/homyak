"""summary + score колонки (LLM-обогащение Phase 4)

Revision ID: 0004_llm_summary_score
Revises: 0003_embedding_index
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_llm_summary_score"
down_revision = "0003_embedding_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_items", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("news_items", sa.Column("score", sa.Float(), nullable=True))
    op.create_index(
        "idx_news_score", "news_items", [sa.text("score DESC NULLS LAST")]
    )


def downgrade() -> None:
    op.drop_index("idx_news_score", table_name="news_items")
    op.drop_column("news_items", "score")
    op.drop_column("news_items", "summary")
