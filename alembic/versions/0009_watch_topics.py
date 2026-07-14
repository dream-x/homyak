"""watch_topics: трендовые темы под пристальное внимание (вотчлист)

Revision ID: 0009_watch_topics
Revises: 0008_insight_score
Create Date: 2026-07-14

news_items.watch_topics — массив имён тем, под которые попал айтем (ChatGPT/Iran/Нефть/…).
Ставит analyzer watchlist по config/watchlist.yaml. GIN-индекс для ленты/панелей по темам.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_watch_topics"
down_revision = "0008_insight_score"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "news_items",
        sa.Column(
            "watch_topics",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_index(
        "idx_news_watch",
        "news_items",
        ["watch_topics"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_news_watch", table_name="news_items")
    op.drop_column("news_items", "watch_topics")
