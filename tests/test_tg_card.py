"""Карточка Telegram: лимит 4096 — жёсткий, его нарушение роняет отправку."""

from dataclasses import dataclass, field

from homyak.adapters.outputs.tg_bot import _TG_LIMIT, _fmt


@dataclass
class _Item:
    title: str = "Заголовок"
    summary: str | None = None
    url: str | None = "https://example.com/a"
    personal_score: float = 0.9
    vertical: str = "it"
    tags: list = field(default_factory=lambda: ["ai", "llm"])
    watch_topics: list = field(default_factory=list)
    feed_name: str = "tw_karpathy"
    source_type: str = "rss"
    author: str | None = None
    insight_score: float | None = None


def test_card_never_exceeds_telegram_limit():
    """Регресс: summary ничем не ограничен (Text-колонка, без num_predict) — карточка
    >4096 роняла отправку: в лентах рвался цикл, в пушах айтем терялся МОЛЧА."""
    monster = _Item(summary="Длинное саммари с апострофом it's и кавычкой \". " * 300)
    card = _fmt(monster)
    assert len(card) <= _TG_LIMIT, f"карточка {len(card)} > лимита {_TG_LIMIT}"


def test_long_card_keeps_link_and_tags():
    """Режем саммари, а не хвост — иначе теряются ссылка на источник и хэштеги."""
    card = _fmt(_Item(summary="а" * 9000))
    assert "#ai" in card and "#llm" in card, "хэштеги потеряны при обрезке"
    assert "karpathy" in card, "источник потерян при обрезке"
    assert "example.com" in card, "ссылка потеряна при обрезке"


def test_short_card_untouched():
    card = _fmt(_Item(summary="Коротко и по делу."))
    assert "Коротко и по делу." in card
    assert "…" not in card
