from homyak.core.textutils import clean_title, strip_html


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Hello &amp; <b>world</b></p>") == "Hello & world"


def test_strip_html_none_on_empty_and_junk():
    assert strip_html(None) is None
    assert strip_html("   ") is None
    assert strip_html("<a href='x'>Comments</a>") is None  # HN-заглушка


def test_clean_title_strips_anchor_from_title():
    # fiercebiotech кладёт <a>…</a> прямо в <title> — тег не должен просочиться
    raw = '<a href="https://x/arpa-h" hreflang="en">ARPA-H awards up to $160M</a>'
    assert clean_title(raw) == "ARPA-H awards up to $160M"


def test_clean_title_keeps_junklike_words():
    # в отличие от strip_html, заголовок не зануляем на junk-словах
    assert clean_title("Comments") == "Comments"
    assert clean_title("<b>Link</b>") == "Link"


def test_clean_title_none_on_empty():
    assert clean_title(None) is None
    assert clean_title("  <br> ") is None


def test_hashtags_basic_and_hyphen():
    from homyak.core.textutils import hashtags

    # дефис Telegram не понимает → подчёркивание
    assert hashtags(["ai", "ai-agents", "python"]) == "#ai #ai_agents #python"


def test_hashtags_cyrillic_and_dedup():
    from homyak.core.textutils import hashtags

    assert hashtags(["нефть", "нефть", "рынок россии"]) == "#нефть #рынок_россии"


def test_hashtags_drops_digit_start_and_empty():
    from homyak.core.textutils import hashtags

    assert hashtags(["3d", "ok"]) == "#ok"  # тег с цифры Telegram не подсветит
    assert hashtags(None) == ""
    assert hashtags(["", "  ", "---"]) == ""


def test_hashtags_limit():
    from homyak.core.textutils import hashtags

    assert hashtags(["a", "b", "c", "d", "e", "f"], limit=3) == "#a #b #c"
