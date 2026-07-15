from homyak.core.watchlist import compile_watchlist, match

WL = compile_watchlist(
    [
        {"name": "Iran", "aliases": ["iran", "iranian", "иран", "иранск"]},
        {"name": "Нефть", "aliases": ["oil", "brent", "нефть", "нефт", "opec"]},
        {"name": "RUB/USD", "aliases": ["usd/rub", "ruble", "рубл"]},
        {"name": "Claude", "aliases": ["claude", "anthropic"]},
    ]
)


def _m(title, text=""):
    return set(match(title, text, WL))


def test_english_entity_and_word_boundary():
    assert "Iran" in _m("Iran nuclear talks resume")
    assert "Iran" in _m("Iranian officials met")  # префиксный матч
    assert "Iran" not in _m("The terrain was rough")  # \b не даст 'iran' в 'terrain'


def test_russian_stems():
    assert "Нефть" in _m("Цены на нефть растут")
    assert "Нефть" in _m("Нефтяной рынок лихорадит")  # стемм 'нефт'
    assert "RUB/USD" in _m("Курс рубля упал")  # 'рубл' → 'рубля'


def test_phrase_and_slash():
    assert "RUB/USD" in _m("Пара USD/RUB выросла")
    assert "Нефть" in _m("OPEC cuts output")


def test_no_false_soil():
    assert "Нефть" not in _m("Rich soil for farming")  # 'oil' в 'soil' не матчится


def test_multiple_and_empty():
    got = _m("Iran boosts oil exports", "")
    assert {"Iran", "Нефть"} <= got
    assert _m("", "") == set()


def test_nature_topic_avoids_false_friends():
    """Ловушки: 'град' не должен ловить 'градус', 'стихи' — стихотворения."""
    from homyak.core.watchlist import load_watchlist, match

    wl = load_watchlist("config/watchlist.yaml")
    assert "Природные явления" in match("МЧС предупредило о ливнях и урагане", "", wl)
    assert "Природные явления" in match("Hurricane makes landfall in Florida", "", wl)
    # 'температура 5 градусов' и стихи — НЕ стихия
    assert "Природные явления" not in match("Температура выросла на 5 градусов", "", wl)
    assert "Природные явления" not in match("Поэт прочитал свои стихи", "", wl)


def test_nature_is_tracked_but_not_boosted():
    from homyak.core.watchlist import no_boost_topics

    assert "Природные явления" in no_boost_topics("config/watchlist.yaml")
    assert "Нефть" not in no_boost_topics("config/watchlist.yaml")  # остальные бустятся
