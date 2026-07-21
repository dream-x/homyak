import pytest

from homyak.adapters.analyzers.title_gen import make_title
from homyak.core.titles import MAX_LEN, derive_title, title_from_url


def test_none_and_empty():
    assert derive_title(None) is None
    assert derive_title("   ") is None
    assert derive_title("", fallback="Запись @ch") == "Запись @ch"
    assert derive_title(None, fallback="Запись @ch") == "Запись @ch"


def test_first_line_is_the_title():
    text = "Можно ли обучать нейросеть без backprop?\n\nНовая работа Diffusing Blame проверяет…"
    assert derive_title(text) == "Можно ли обучать нейросеть без backprop?"


def test_short_single_line_idempotent():
    t = "Paul Graham: they can't stop spending."
    assert derive_title(t) == t
    assert derive_title(derive_title(t)) == derive_title(t)


def test_strips_markdown_links_marks_and_urls():
    text = "**CLI-Anything**: дать агентам руки — [подробнее](https://x.io/a) https://t.me/c"
    got = derive_title(text)
    assert got == "CLI-Anything: дать агентам руки — подробнее"
    assert "http" not in got and "*" not in got and "[" not in got


def test_strips_leading_emoji_and_bullets():
    assert derive_title("🚀 Запуск нового сервиса") == "Запуск нового сервиса"
    assert derive_title("• Пункт списка") == "Пункт списка"


def test_long_line_cut_at_sentence():
    text = "DoorDash тихо выкатили dd-cli. " + "Дальше идёт длинное продолжение " * 5
    got = derive_title(text)
    assert got == "DoorDash тихо выкатили dd-cli."


def test_long_line_without_sentence_cut_at_word_with_ellipsis():
    text = "слово " * 40  # длинная строка без знаков конца предложения
    got = derive_title(text)
    assert len(got) <= MAX_LEN + 1
    assert got.endswith("…")
    assert not got.endswith(" …")


def test_media_only_falls_back():
    assert derive_title("", fallback="Запись @cgevent") == "Запись @cgevent"


# --- make_title (LLM-генерация с фолбэком на эвристику) ---

LONG = "Разработчики выкатили новый инструмент. " * 4  # >80 симв → пойдёт в LLM


class _FakeLLM:
    def __init__(self, reply=None, boom=False):
        self.reply, self.boom, self.calls = reply, boom, 0

    async def chat_text(self, system, user, think=None):
        self.calls += 1
        if self.boom:
            raise RuntimeError("llm down")
        return self.reply


@pytest.mark.asyncio
async def test_make_title_uses_llm_and_cleans_output():
    llm = _FakeLLM(reply='  "Новый CLI для агентов".  ')
    got = await make_title(llm, LONG, "@ch")
    assert got == "Новый CLI для агентов"  # кавычки, точка и пробелы убраны
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_make_title_strips_think_leak():
    llm = _FakeLLM(reply="<think>надо покороче</think>\nDoorDash запустил dd-cli")
    assert await make_title(llm, LONG, "@ch") == "DoorDash запустил dd-cli"


@pytest.mark.asyncio
async def test_make_title_falls_back_to_heuristic_on_llm_error():
    llm = _FakeLLM(boom=True)
    text = "Первая строка как заголовок\n\nдальше тело поста подлиннее чем восемьдесят символов ровно"
    assert await make_title(llm, text, "@ch") == "Первая строка как заголовок"


@pytest.mark.asyncio
async def test_make_title_skips_llm_for_short_text():
    llm = _FakeLLM(reply="не должно вызваться")
    got = await make_title(llm, "Короткий пост", "@ch")
    assert got == "Короткий пост"  # эвристика, а не LLM
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_make_title_no_text_returns_fallback_without_llm():
    llm = _FakeLLM(reply="x")
    assert await make_title(llm, "", "@cgevent") == "Запись @cgevent"
    assert llm.calls == 0


# --- title_from_url (голые ссылки) ---


def test_title_from_url_slug():
    assert (
        title_from_url("https://www.group-ib.com/blog/clicklock-stealer-macos-malware/")
        == "Clicklock stealer macos malware"
    )
    assert title_from_url("https://github.com/xai-org/grok-build") == "Grok build"
    assert title_from_url("https://example.com/2026/07/my_post.html") == "My post"


def test_title_from_url_root_or_junk_is_none():
    assert title_from_url("https://example.com/") is None
    assert title_from_url("https://example.com/blog/12345") is None  # id + stop-сегмент
    assert title_from_url("not a url") is None
    assert title_from_url(None) is None


@pytest.mark.asyncio
async def test_make_title_bare_link_uses_url_slug():
    # пост — только ссылка: заголовок из слага, а не «Запись @…»
    got = await make_title(None, "https://www.group-ib.com/blog/clicklock-stealer-macos-malware/", "@ch")
    assert got == "Clicklock stealer macos malware"


@pytest.mark.asyncio
async def test_make_title_link_with_no_slug_falls_back_to_channel():
    got = await make_title(None, "https://example.com/", "@cgevent")
    assert got == "Запись @cgevent"
