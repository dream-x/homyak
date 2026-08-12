"""Заземление ⭐-карточки и выбор режима — чистая логика без LLM."""

from datetime import datetime

from homyak.core.starcard import (
    Card,
    _parse,
    choose_mode,
    facts,
    filter_grounded,
    from_data,
    ungrounded,
)
from homyak.pipeline.starchan import due_hour, parse_hours, render_card


def test_facts_takes_names_and_numbers_not_russian_words():
    f = facts("Rust ускорил сборку на 40% в версии 1.75")
    assert "rust" in f and "40" in f and "1.75" in f
    assert "ускорил" not in f  # кириллицу не проверяем: пересказ обязан переформулировать


def test_generic_latin_is_not_a_claim():
    # «open source», «API» живут в русской технической речи сами по себе
    assert not ungrounded("Проект open source, отдаёт API", "Проект про базы данных")


def test_invented_number_is_caught():
    src = "The build got faster after the rewrite."
    assert ungrounded("Сборка ускорилась на 40%", src) == {"40"}


def test_number_is_not_matched_as_substring():
    """«5» не должно найтись внутри «2025» — иначе выдуманная цифра пройдёт как подтверждённая."""
    assert ungrounded("выросло в 5 раз", "релиз 2025 года") == {"5"}


def test_invented_product_name_is_caught():
    src = "Anthropic released a new model for developers."
    assert ungrounded("Модель от OpenAI обошла конкурентов", src) == {"openai"}


def test_name_matches_as_substring_of_source():
    # Qwen ≈ qwen3.6, github ≈ github.com — морфология и версии не должны считаться выдумкой
    assert not ungrounded("Qwen выложили на GitHub", "qwen3.6 published on github.com/foo")


def test_filter_drops_only_the_bad_phrase():
    card = Card(
        line="Команда переписала парсер на Rust",
        points=["Сборка ускорилась на 40%", "Парсер переносим между платформами"],
        mode="full",
    )
    out = filter_grounded(card, "The team rewrote the parser in Rust. It is portable across platforms.")
    assert out.line == card.line
    assert out.points == ["Парсер переносим между платформами"]  # выдуманные 40% выброшены
    assert out.dropped and "40" in out.dropped[0]


def test_ungrounded_line_degrades_whole_card():
    """Линия — стержень: без неё тезисы висят в воздухе, карточка становится голой."""
    card = Card(line="OpenAI купила стартап", points=["Парсер на Rust"], mode="full")
    out = filter_grounded(card, "The team rewrote the parser in Rust.")
    assert out.mode == "bare" and out.line is None and out.points == []


def test_choose_mode_by_length():
    assert choose_mode("x" * 900) == "full"
    assert choose_mode("x" * 200) == "brief"
    assert choose_mode("x" * 10) == "bare"
    assert choose_mode(None) == "bare"


def test_parse_tolerates_think_and_prose_around_json():
    raw = '<think>рассуждения</think> Вот ответ: {"line": "Суть", "points": ["- А", "• Б"]} спасибо'
    card = _parse(raw)
    assert card.line == "Суть" and card.points == ["А", "Б"]


def test_parse_survives_garbage():
    assert not _parse("модель заболталась без JSON").has_text


def test_parse_caps_points():
    card = _parse('{"line": "x", "points": ["1", "2", "3", "4", "5"]}')
    assert len(card.points) == 3


def test_from_data_tolerates_points_as_one_string():
    card = from_data({"line": "Суть", "points": "• А\n• Б"})
    assert card.points == ["А", "Б"]


def test_from_data_tolerates_wrong_types():
    assert from_data({"line": None, "points": 42}).has_text is False


def test_hyphenated_latin_is_not_a_fabrication():
    """«AI-агент» давал токен «ai-», которого нет ни в одном тексте, и убивал живую фразу."""
    assert not ungrounded("Запускает AI-агентов и API-вызовы", "The agent calls tools")


# Ниже — ложные срабатывания, пойманные на живых ⭐ (записи #126594): типографика исходника,
# а не выдумка модели. Оба «факта» в тексте есть, просто записаны иначе.


def test_thin_space_in_thousands_is_still_the_same_number():
    assert not ungrounded("1290 характеристик на профиль", "— 1 290 характеристик на каждого")


def test_non_breaking_hyphen_in_a_product_name():
    assert not ungrounded("Прогнали тесты на GPT-5.5", "Авторы прогнали испытания на GPT‑5.5")


def test_number_glued_differently_still_counts():
    assert not ungrounded("8.3 млрд профилей", "8,3 млрд персона-профилей")


def test_number_spelled_out_in_the_source_counts_as_the_digit():
    """Английские статьи пишут «four remained», пересказ — «4». Это один и тот же факт."""
    assert not ungrounded("4 запроса остались открытыми", "four requests remained open")
    assert not ungrounded("срабатывает после 3 вызовов", "after three ordinary tool calls")
    assert not ungrounded("команда из 5 человек", "команда из пяти человек")


def test_spelled_out_numbers_do_not_legalize_other_digits():
    assert ungrounded("выросло на 40%", "four requests remained open") == {"40"}


# --- расписание дайджеста ---


def test_digest_hours_parsed_and_sanitized():
    assert parse_hours("10,23") == [10, 23]
    assert parse_hours(" 23, 10 ,10 ") == [10, 23]  # дубли и пробелы
    assert parse_hours("25,-1,abc") == []  # мусор молча отбрасывается
    assert parse_hours("") == []


def test_digest_fires_once_per_slot():
    hours = [10, 23]
    at_11 = datetime(2026, 8, 13, 11, 5)
    assert due_hour(at_11, hours, {}) == 10
    state = {"date": "2026-08-13", "hours": [10]}
    assert due_hour(at_11, hours, state) is None  # уже слали
    assert due_hour(datetime(2026, 8, 13, 23, 30), hours, state) == 23


def test_missed_slot_is_sent_later_the_same_day():
    """Сервис лежал в 10:00 — дайджест уходит, когда поднялся, а не пропадает."""
    assert due_hour(datetime(2026, 8, 13, 14, 0), [10, 23], {}) == 10


def test_new_day_resets_state():
    stale = {"date": "2026-08-12", "hours": [10, 23]}
    assert due_hour(datetime(2026, 8, 13, 10, 1), [10, 23], stale) == 10


def test_nothing_due_before_the_first_slot():
    assert due_hour(datetime(2026, 8, 13, 9, 59), [10, 23], {}) is None


# --- отрисовка карточки: уходит в публичный канал, ошибки видны всем ---


class _Item:
    id = 1
    title = "Rust <3 & co"
    url = "https://example.com/a?x=1&y=2"
    source_type = "rss"
    feed_name = "lobsters_hot"
    tags = ["rust", "performance"]
    published_at = None
    fetched_at = None


def test_render_escapes_html_in_title_and_url():
    """Заголовок с '<' или '&' без экранирования ломает parse_mode=HTML — пост не уйдёт."""
    out = render_card(_Item(), Card(line="Суть", points=["Тезис"], mode="full"))
    assert "Rust &lt;3 &amp; co" in out
    assert "x=1&amp;y=2" in out
    assert out.startswith("⭐ <a href=")


def test_render_bare_card_has_link_and_no_empty_body():
    out = render_card(_Item(), Card(mode="bare"))
    assert "example.com" in out and "•" not in out


def test_render_truncates_to_telegram_limit():
    huge = Card(line="д" * 5000, points=["ц" * 3000], mode="full")
    assert len(render_card(_Item(), huge)) <= 3900
