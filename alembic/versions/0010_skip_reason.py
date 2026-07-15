"""skip_reason: почему айтем отсеян гейтом предобработки (не дошёл до LLM)

Revision ID: 0010_skip_reason
Revises: 0009_watch_topics
Create Date: 2026-07-15

Гейт (prefilter) режет мусор до дорогих LLM-стадий. Причину пишем сюда, чтобы на дашборде
было видно, ЧТО и ПОЧЕМУ отсеяли — иначе новости терялись бы молча.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_skip_reason"
down_revision = "0009_watch_topics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_items", sa.Column("skip_reason", sa.Text(), nullable=True))
    op.create_index(
        "idx_news_skipped",
        "news_items",
        ["skip_reason"],
        postgresql_where=sa.text("skip_reason IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_news_skipped", table_name="news_items")
    op.drop_column("news_items", "skip_reason")
