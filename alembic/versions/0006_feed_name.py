"""feed_name — какой фид/источник принёс item (для фильтрации и source-affinity по фиду)

Revision ID: 0006_feed_name
Revises: 0005_personalization
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_feed_name"
down_revision = "0005_personalization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_items", sa.Column("feed_name", sa.String(64), nullable=True))
    op.create_index(
        "idx_news_feed",
        "news_items",
        ["feed_name"],
        postgresql_where=sa.text("feed_name IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_news_feed", table_name="news_items")
    op.drop_column("news_items", "feed_name")
