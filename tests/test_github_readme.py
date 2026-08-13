"""README репозитория как источник текста: разбор ссылки и чистка markdown."""

from homyak.core.github import clean_markdown, repo_of
from homyak.core.starcard import make_card


def test_repo_of_accepts_the_shapes_github_links_actually_take():
    assert repo_of("https://github.com/jaylfc/taOS") == ("jaylfc", "taOS")
    assert repo_of("https://github.com/meilisearch/meilisearch/") == ("meilisearch", "meilisearch")
    assert repo_of("https://github.com/owner/repo/tree/main/src") == ("owner", "repo")
    assert repo_of("https://github.com/owner/repo.git") == ("owner", "repo")
    assert repo_of("https://www.github.com/owner/repo#readme") == ("owner", "repo")


def test_repo_of_rejects_non_repos():
    """У /trending и /features README нет — запрос к API вернул бы 404 на каждой такой ссылке."""
    assert repo_of("https://github.com/trending/rust") is None
    assert repo_of("https://github.com/features/copilot") is None
    assert repo_of("https://github.com/simonw") is None  # профиль, не репозиторий
    assert repo_of("https://gitlab.com/owner/repo") is None
    assert repo_of("https://news.ycombinator.com/item?id=1") is None
    assert repo_of(None) is None


def test_clean_markdown_drops_the_badge_wall():
    """Верх почти каждого README — бейджи и картинки: контекст модели они съедают, смысла ноль."""
    md = (
        "# taOS\n"
        "[![CI](https://img.shields.io/badge/ci-passing.svg)](https://ci.example.com)\n"
        "![logo](docs/logo.png)\n\n"
        "<!-- скрытый комментарий -->\n"
        "<p align=center>Агентная ОС</p>\n\n"
        "Смотри [документацию](https://docs.example.com) для деталей.\n"
    )
    out = clean_markdown(md)
    assert "img.shields.io" not in out and "docs/logo.png" not in out
    assert "скрытый комментарий" not in out and "<p align" not in out
    assert "Агентная ОС" in out
    assert "Смотри документацию для деталей." in out  # ссылка осталась своим текстом


def test_clean_markdown_keeps_prose_readable():
    out = clean_markdown("# Заголовок\n\n> цитата\n\nОбычный   текст.\n\n\n\nЕщё абзац.")
    assert out.startswith("Заголовок")
    assert "Обычный текст." in out
    assert "\n\n\n" not in out


class _DeadLLM:
    async def chat_json(self, system, user):
        raise TimeoutError("бокс не ответил")


async def test_llm_outage_is_not_the_same_as_having_no_text():
    """Текст есть, модель молчит — карточку публиковать нельзя, надо ретраить.

    Первый живой пост ушёл голым именно так: README скачался на 60 КБ, а перегруженный
    GPU-бокс не ответил за таймаут.
    """
    card = await make_card("taOS", "x" * 2000, _DeadLLM())
    assert card.mode == "failed"


async def test_no_text_is_a_final_verdict_and_never_calls_the_model():
    card = await make_card("твит", "коротко", _DeadLLM())  # упал бы, если бы дёрнул LLM
    assert card.mode == "bare"
