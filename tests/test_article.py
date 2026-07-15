from homyak.core.article import _strip_reader


def test_strip_reader_takes_body_after_marker():
    md = "Title: X\n\nURL Source: https://e.com\n\nMarkdown Content:\nРеальный текст статьи."
    assert _strip_reader(md) == "Реальный текст статьи."


def test_strip_reader_passthrough_without_marker():
    assert _strip_reader("просто текст") == "просто текст"


def test_strip_reader_none_and_empty():
    assert _strip_reader(None) is None
    assert _strip_reader("   ") is None


def test_skipped_hosts():
    from homyak.core.article import _skipped

    assert _skipped("https://www.investing.com/news/viavi-x")  # жёсткая стена
    assert not _skipped("https://www.phoronix.com/news/x")


def test_skip_matches_host_boundary_not_substring():
    from homyak.core.article import _skipped

    assert _skipped("https://x.com/ThePrimeagen/status/1")  # сам x.com
    assert _skipped("https://mobile.x.com/a")  # поддомен
    # регресс: 'x.com' НЕ должен ловить phoronix.com / netflix.com
    assert not _skipped("https://www.phoronix.com/news/x")
    assert not _skipped("https://netflix.com/title/1")
