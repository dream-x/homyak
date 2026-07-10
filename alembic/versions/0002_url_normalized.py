"""url_normalized column + index (для URL-дедупликации)

Revision ID: 0002_url_normalized
Revises: 0001_initial
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_url_normalized"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_items", sa.Column("url_normalized", sa.Text(), nullable=True))
    op.create_index(
        "idx_news_url_normalized",
        "news_items",
        ["url_normalized"],
        postgresql_where=sa.text("url_normalized IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_news_url_normalized", table_name="news_items")
    op.drop_column("news_items", "url_normalized")
