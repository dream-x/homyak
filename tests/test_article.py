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


# --- Reddit и og-разметка: поймано на живой ссылке из бота ---


def test_reddit_is_rewritten_to_the_old_interface():
    """Новый Reddit отдаёт JS-заглушку на 8 КБ без единого og-тега; old — серверный HTML."""
    from homyak.core.article import _rewrite

    assert _rewrite("https://www.reddit.com/r/LocalLLaMA/s/8GSuHDbTtU") == \
        "https://old.reddit.com/r/LocalLLaMA/s/8GSuHDbTtU"
    assert _rewrite("https://reddit.com/r/rust/comments/1") == \
        "https://old.reddit.com/r/rust/comments/1"
    # чужие хосты не трогаем, в том числе похожие по имени
    assert _rewrite("https://notreddit.com/x") == "https://notreddit.com/x"
    assert _rewrite("https://old.reddit.com/r/x") == "https://old.reddit.com/r/x"


def test_meta_is_read_in_both_attribute_orders():
    """Порядок property/content в разметке произволен — шаблона два не для красоты."""
    from homyak.core.article import _meta

    a = '<meta property="og:title" content="Заголовок статьи">'
    b = '<meta content="Другой заголовок" property="og:title">'
    assert _meta(a, "og:title") == "Заголовок статьи"
    assert _meta(b, "og:title") == "Другой заголовок"
    assert _meta(a, "og:description") is None


def test_meta_unescapes_entities():
    from homyak.core.article import _meta

    html = '<meta property="og:title" content="Rust &amp; Go: 1&#39;000 запросов">'
    assert _meta(html, "og:title") == "Rust & Go: 1'000 запросов"


def test_tweet_url_is_recognised_across_hosts():
    from homyak.core.article import _TWEET_RE

    for u in ("https://x.com/ClaudeDevs/status/2089798442306711646",
              "https://twitter.com/ClaudeDevs/status/123",
              "https://mobile.twitter.com/user/status/456",
              "https://www.x.com/user/status/789"):
        assert _TWEET_RE.match(u), u
    for u in ("https://x.com/ClaudeDevs", "https://example.com/x/status/1"):
        assert _TWEET_RE.match(u) is None, u
