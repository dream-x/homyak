"""idx_news_embedding_version (для backfill-скана при смене модели)

Revision ID: 0003_embedding_index
Revises: 0002_url_normalized
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_embedding_index"
down_revision = "0002_url_normalized"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_news_embedding_version",
        "news_items",
        ["embedding_version"],
        postgresql_where=sa.text("embedding_version IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_news_embedding_version", table_name="news_items")
