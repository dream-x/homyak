"""star_posted_at: айтем опубликован в ⭐-канал (дедуп от повторной публикации)

Revision ID: 0014_star_posted
Revises: 0013_channel_posted
Create Date: 2026-08-13

Третий независимый маркер доставки рядом с pushed_at (личный DM) и channel_posted_at
(лента-канал): ⭐-канал собирает отмеченное вручную, и одна запись может законно попасть
во все три. Без своего флага повторная звезда (снял/поставил) или переезд consumer'а на
другой durable выдали бы дубль в канал.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_star_posted"
down_revision = "0013_channel_posted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "news_items", sa.Column("star_posted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("news_items", "star_posted_at")
