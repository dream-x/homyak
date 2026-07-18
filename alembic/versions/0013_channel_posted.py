"""channel_posted_at: айтем опубликован в лента-канал (дедуп от повторной публикации)

Revision ID: 0013_channel_posted
Revises: 0012_feedback_topic_unique
Create Date: 2026-07-18

Отдельный маркер от pushed_at: личный пуш в DM и публикация в общий канал независимы —
айтем может уйти в DM владельцу (по его pushonly/порогу) и при этом отдельно попасть в
канал-ленту (все вертикали, свой порог). Без своего флага канал дублировал бы посты при
повторной доставке события processed или прогоне sweeper'а.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_channel_posted"
down_revision = "0012_feedback_topic_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "news_items", sa.Column("channel_posted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("news_items", "channel_posted_at")
