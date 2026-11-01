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


def test_card_links_wiki_pages(monkeypatch):
    """Совпавшие страницы вики — строкой «📚 в базе знаний» со ссылками; пусто → строки нет."""
    import homyak.adapters.outputs.tg_bot as bot

    pages = [
        {"kind": "entities", "slug": "ollama", "title": "ollama", "n": 9, "url": "http://h/wiki?kind=entities&slug=ollama"},
        {"kind": "concepts", "slug": "self-hosting", "title": "self hosting", "n": 2, "url": "http://h/wiki?kind=concepts&slug=self-hosting"},
    ]
    monkeypatch.setattr(bot, "related_pages", lambda title, tags: pages)
    card = _fmt(_Item(summary="x"))
    assert '📚 в базе знаний: <a href="http://h/wiki?kind=entities&amp;slug=ollama">ollama</a>·9, ' in card
    assert "self hosting</a>·2" in card
    assert "📚" not in _fmt(_Item(summary="x"), wiki=False)  # канальный пост — без твоей вики
    # длинная карточка режет саммари, а вика остаётся в подвале
    long_card = _fmt(_Item(summary="б" * 9000))
    assert len(long_card) <= _TG_LIMIT and "📚 в базе знаний" in long_card

    monkeypatch.setattr(bot, "related_pages", lambda title, tags: [])
    assert "📚" not in _fmt(_Item(summary="x"))
