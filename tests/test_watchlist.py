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
