"""тематические вертикали: news_items.vertical + профиль/аффинити/taste per-vertical

Revision ID: 0007_verticals
Revises: 0006_feed_name
Create Date: 2026-07-13

Существующие профиль/веса/taste мигрируют в вертикаль 'it' (server_default).
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_verticals"
down_revision = "0006_feed_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. news_items.vertical
    op.add_column("news_items", sa.Column("vertical", sa.String(16), nullable=True))
    op.create_index(
        "idx_news_vertical",
        "news_items",
        ["vertical"],
        postgresql_where=sa.text("vertical IS NOT NULL"),
    )

    # 2. profile per-vertical (один активный на вертикаль)
    op.add_column(
        "profile", sa.Column("vertical", sa.String(16), nullable=False, server_default="it")
    )
    op.drop_index("idx_profile_active", table_name="profile")
    op.create_index(
        "idx_profile_active_vertical",
        "profile",
        ["vertical"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    # 3. tag_affinity per-vertical (PK: vertical, tag)
    op.add_column(
        "tag_affinity", sa.Column("vertical", sa.String(16), nullable=False, server_default="it")
    )
    op.drop_constraint("tag_affinity_pkey", "tag_affinity", type_="primary")
    op.create_primary_key("tag_affinity_pkey", "tag_affinity", ["vertical", "tag"])

    # 4. source_affinity per-vertical (PK: vertical, source_type, author)
    op.add_column(
        "source_affinity",
        sa.Column("vertical", sa.String(16), nullable=False, server_default="it"),
    )
    op.drop_constraint("source_affinity_pkey", "source_affinity", type_="primary")
    op.create_primary_key(
        "source_affinity_pkey", "source_affinity", ["vertical", "source_type", "author"]
    )

    # 5. taste_state per-vertical (PK: vertical)
    op.add_column(
        "taste_state", sa.Column("vertical", sa.String(16), nullable=False, server_default="it")
    )
    op.drop_constraint("ck_taste_singleton", "taste_state", type_="check")
    op.drop_constraint("taste_state_pkey", "taste_state", type_="primary")
    op.drop_column("taste_state", "id")
    op.create_primary_key("taste_state_pkey", "taste_state", ["vertical"])


def downgrade() -> None:
    op.add_column("taste_state", sa.Column("id", sa.Integer(), nullable=True))
    op.execute("UPDATE taste_state SET id = 1 WHERE vertical = 'it'")
    op.execute("DELETE FROM taste_state WHERE vertical <> 'it'")
    op.drop_constraint("taste_state_pkey", "taste_state", type_="primary")
    op.create_primary_key("taste_state_pkey", "taste_state", ["id"])
    op.create_check_constraint("ck_taste_singleton", "taste_state", "id = 1")
    op.drop_column("taste_state", "vertical")

    op.drop_constraint("source_affinity_pkey", "source_affinity", type_="primary")
    op.execute("DELETE FROM source_affinity WHERE vertical <> 'it'")
    op.create_primary_key("source_affinity_pkey", "source_affinity", ["source_type", "author"])
    op.drop_column("source_affinity", "vertical")

    op.drop_constraint("tag_affinity_pkey", "tag_affinity", type_="primary")
    op.execute("DELETE FROM tag_affinity WHERE vertical <> 'it'")
    op.create_primary_key("tag_affinity_pkey", "tag_affinity", ["tag"])
    op.drop_column("tag_affinity", "vertical")

    op.drop_index("idx_profile_active_vertical", table_name="profile")
    op.execute("DELETE FROM profile WHERE vertical <> 'it'")
    op.create_index("idx_profile_active", "profile", ["active"], unique=True, postgresql_where=sa.text("active"))
    op.drop_column("profile", "vertical")

    op.drop_index("idx_news_vertical", table_name="news_items")
    op.drop_column("news_items", "vertical")
