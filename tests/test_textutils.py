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


def test_detect_lang():
    from homyak.core.textutils import detect_lang

    assert detect_lang("Разработчики выкатили новый фреймворк на Rust") == "ru"
    assert detect_lang("A team released a new Rust framework") == "en"
    # русская статья с кодом/терминами латиницей — всё равно ru
    assert detect_lang("Переписали планировщик: continuous batching и paged attention, throughput вырос") == "ru"
    # прочие языки (латиница/пусто) → en
    assert detect_lang("Ein neues Framework für maschinelles Lernen") == "en"
    assert detect_lang("") == "en"
    assert detect_lang(None) == "en"
    assert detect_lang("12345 !!!") == "en"


def test_fmt_age():
    from homyak.core.textutils import fmt_age

    assert fmt_age(None) == ""
    assert fmt_age(0) == "только что"
    assert fmt_age(59) == "только что"
    assert fmt_age(60) == "1м"
    assert fmt_age(59 * 60) == "59м"
    assert fmt_age(3600) == "1ч"
    assert fmt_age(23 * 3600) == "23ч"
    assert fmt_age(24 * 3600) == "1д"
    assert fmt_age(13 * 24 * 3600) == "13д"
    assert fmt_age(21 * 24 * 3600) == "3нед"
    assert fmt_age(-5) == "только что"  # часы на источнике убежали вперёд — не «-1м»
