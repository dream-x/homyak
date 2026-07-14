"""insight_score: детектор инсайтов на посте (0..1), + индекс для ленты 💡 Insights

Revision ID: 0008_insight_score
Revises: 0007_verticals
Create Date: 2026-07-14

insight_score выставляет llm_tagger в том же LLM-вызове (без доп. нагрузки):
высоко — реальный тезис/аргумент/данные/прогноз; низко — заголовок/ссылка/PR/ретвит.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_insight_score"
down_revision = "0007_verticals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_items", sa.Column("insight_score", sa.Float(), nullable=True))
    op.create_index(
        "idx_news_insight",
        "news_items",
        [sa.text("insight_score DESC NULLS LAST")],
        postgresql_where=sa.text("insight_score IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_news_insight", table_name="news_items")
    op.drop_column("news_items", "insight_score")
