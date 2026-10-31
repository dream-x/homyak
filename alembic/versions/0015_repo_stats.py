"""repo_stats: дневные снимки звёзд/форков GitHub-репозиториев, попавших в ленту

Revision ID: 0015_repo_stats
Revises: 0014_star_posted
Create Date: 2026-10-31

GitHub-радар: лента приносит репозитории (trending/search/starred через RSSHub и ссылки в
постах), но не говорит, растут ли они. Sweeper раз в сутки снимает stars/forks по тем, что
были в ленте за месяц и по всем ⭐-помеченным; радар считает скорость (звёзд/день за окно).
Ключ (repo, day): повторный снимок за день перезаписывает, а не дублирует.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_repo_stats"
down_revision = "0014_star_posted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repo_stats",
        sa.Column("repo", sa.Text(), nullable=False),  # owner/name, lower-case
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("forks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_issues", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("repo", "day", name="pk_repo_stats"),
    )
    op.create_index("idx_repo_stats_day", "repo_stats", [sa.text("day DESC")])


def downgrade() -> None:
    op.drop_index("idx_repo_stats_day", table_name="repo_stats")
    op.drop_table("repo_stats")
