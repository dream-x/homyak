from homyak.core.article import _strip_reader


def test_strip_reader_takes_body_after_marker():
    md = "Title: X\n\nURL Source: https://e.com\n\nMarkdown Content:\nРеальный текст статьи."
    assert _strip_reader(md) == "Реальный текст статьи."


def test_strip_reader_passthrough_without_marker():
    assert _strip_reader("просто текст") == "просто текст"


def test_strip_reader_none_and_empty():
    assert _strip_reader(None) is None
    assert _strip_reader("   ") is None
