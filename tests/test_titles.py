from homyak.core.titles import MAX_LEN, derive_title


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
