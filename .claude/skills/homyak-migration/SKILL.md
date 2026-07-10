---
name: homyak-migration
description: Написать и проверить Alembic-миграцию в Homyak по конвенциям проекта — ручные (не autogenerate) обратимые миграции с postgres-специфичными конструкциями. Используй всегда при изменении схемы БД, добавлении таблиц/колонок/индексов или новой миграции.
---

# Homyak: миграции Alembic

Async-engine (asyncpg). Миграции **ручные**, не `--autogenerate`, чтобы контролировать порядок
создания (циклические FK), postgres-специфику (partial index, GENERATED tsvector, GIN) и обратимость.

## Правила

1. **Именование**: `NNNN_короткое_описание.py` (`0001_initial.py`, `0004_personalization.py`).
   `down_revision` указывает на предыдущую — цепочка не должна ветвиться.
2. **Всегда симметричный `downgrade()`**: порядок обратный upgrade (drop FK → drop index → drop table).
   Миграция обязана проходить `upgrade → downgrade → upgrade` без ошибок.
3. **Циклические FK** создавай отдельным шагом ПОСЛЕ обеих таблиц:
   `op.create_foreign_key(...)` в конце upgrade, `op.drop_constraint(...)` в начале downgrade.
   (Пример: `clusters.representative_id → news_items.id` при том, что `news_items.cluster_id → clusters.id`.)
4. **Postgres-специфика** — через `sa.text(...)` и явные kwargs:
   - Partial index: `op.create_index("idx", "t", ["col"], postgresql_where=sa.text("col IS NULL"))`
   - GIN: `postgresql_using="gin"`
   - GENERATED: `sa.Column("search_tsv", TSVECTOR, sa.Computed("to_tsvector('simple', ...)", persisted=True))`
   - `DESC` в индексе: `op.create_index("idx", "t", [sa.text("published_at DESC")])`
5. **Backfill** новых NOT NULL/вычисляемых колонок делай в той же миграции (data migration) до установки
   constraint'а. Код должен быть безопасен на пустой таблице (noop).
6. **Не роняй данные молча** в downgrade — если drop колонки теряет данные, это ожидаемо для отката,
   но не добавляй неожиданных `DROP`.
7. ORM в `homyak/core/models.py` держи синхронным со схемой (для будущих autogenerate-диффов и типов),
   но истина — в миграции.

## Скелет миграции

```python
"""<описание>"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_personalization"
down_revision = "0003_..."
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("profile", ...)
    op.add_column("news_items", sa.Column("personal_score", sa.Float(), nullable=True))
    op.create_index("idx_news_personal", "news_items",
                    [sa.text("personal_score DESC NULLS LAST")])
    # циклические FK — в конце

def downgrade() -> None:
    op.drop_index("idx_news_personal", table_name="news_items")
    op.drop_column("news_items", "personal_score")
    op.drop_table("profile")
```

## Проверка (обязательно прогнать)

```bash
uv run alembic upgrade head           # применяется чисто
uv run alembic downgrade -1           # откат последней
uv run alembic upgrade head           # повторное применение
# Для оффлайн-ревью SQL без БД:
uv run alembic upgrade <rev> --sql    # печатает SQL, не трогая БД
```

Если Postgres поднят — сверь `psql -c '\d+ <table>'`: индексы, constraints, generated-колонки на месте.
