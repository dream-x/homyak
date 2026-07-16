"""feedback: UNIQUE по (item, signal, topic) вместо (item, signal)

Revision ID: 0012_feedback_topic_unique
Revises: 0011_muted_tags
Create Date: 2026-07-16

Мьют стал двухшаговым (🔇 → выбери тег), и старый UNIQUE(item, signal) сразу выстрелил:
замьютив на одной статье `crypto`, а потом `nft`, пользователь получал «🔇 «crypto» снято»
и `nft` незамьюченным — toggle снимал прежнюю запись, потому что тема в ключ не входила.

NULLS NOT DISTINCT обязателен (Postgres 15+): у 👍/👎/⭐ topic = NULL, а NULL'ы по умолчанию
считаются различными — без этого повторный клик перестал бы отменять фидбек и наплодил бы
дубли вместо toggle.

Данные: (item, signal) был уникален, значит и (item, signal, topic) уникален — конфликтов
при накатке нет. Обратно тоже: сужение ключа не создаёт дублей по (item, signal).
"""

from alembic import op

revision = "0012_feedback_topic_unique"
down_revision = "0011_muted_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_feedback_item_signal", "feedback", type_="unique")
    op.create_unique_constraint(
        "uq_feedback_item_signal_topic",
        "feedback",
        ["news_item_id", "signal", "topic"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # Сначала схлопываем возможные дубли по (item, signal) — их мог наплодить новый ключ
    # (два мьюта разных тегов на одной статье), а старый их не переживёт.
    op.execute(
        "delete from feedback f using feedback g"
        " where f.news_item_id = g.news_item_id and f.signal = g.signal and f.id > g.id"
    )
    op.drop_constraint("uq_feedback_item_signal_topic", "feedback", type_="unique")
    op.create_unique_constraint(
        "uq_feedback_item_signal", "feedback", ["news_item_id", "signal"]
    )
