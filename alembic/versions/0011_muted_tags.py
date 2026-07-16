"""muted_tags: мьюты от кнопки 🔇 отдельно от декларации интересов

Revision ID: 0011_muted_tags
Revises: 0010_skip_reason
Create Date: 2026-07-16

Раньше 🔇 звал set_profile и дописывал mute в profile.topics — кнопка переписывала текст,
который пользователь объявил сам. Мьютился ПЕРВЫЙ тег статьи, а он самый широкий: у медицинской
статьи это `medical`. Одно нажатие выключило 21% вертикали (273 айтема с personal_score=NULL),
и заметить это было неоткуда — hard-mute даже не писал skip_reason.

Теперь мьюты от кнопки живут здесь (слой «выученное»), а config/interests.yaml (слой
«декларация») правит только человек. Таблицу можно очистить без потери декларации.

Данные не переносим: mute'ы, которые сейчас лежат в profile.topics (sports, politics) —
осознанная декларация, их место в interests.yaml. Единственный мьют от кнопки (`medical`)
снят вручную 2026-07-16 вместе с его следом в tag_affinity.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_muted_tags"
down_revision = "0010_skip_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "muted_tags",
        sa.Column("vertical", sa.String(16), nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("vertical", "tag", name="pk_muted_tags"),
    )


def downgrade() -> None:
    op.drop_table("muted_tags")
