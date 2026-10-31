from homyak.core.interests import WatchTopic
from homyak.core.timeline import peaks, pick_unit, resolve_watch, sparkline, tsquery_for


def test_unit_follows_the_range():
    assert pick_unit(7) == "day"
    assert pick_unit(35) == "day"
    assert pick_unit(90) == "week"
    assert pick_unit(365) == "month"


def test_tsquery_is_and_of_words_with_prefixes_for_long_ones():
    assert tsquery_for("Иран санкции") == "иран:* & санкции:*"
    assert tsquery_for("go lang") == "go & lang:*"
    assert tsquery_for("claude claude") == "claude:*"
    assert tsquery_for("!!!") is None
    assert tsquery_for("") is None


def test_watch_topic_resolves_by_name_or_alias():
    topics = [WatchTopic(name="Нефть", aliases=["oil", "brent"]), WatchTopic(name="Claude")]
    assert resolve_watch("нефть", topics) == "Нефть"
    assert resolve_watch("Brent", topics) == "Нефть"
    assert resolve_watch("claude", topics) == "Claude"
    assert resolve_watch("rust", topics) is None
    assert resolve_watch("", topics) is None


def test_peaks_are_triple_the_typical_window():
    assert peaks([1, 1, 2, 9, 1, 0, 1]) == [False, False, False, True, False, False, False]
    assert peaks([0, 0, 5]) == [False, False, False]  # одно ненулевое окно — не с чем сравнивать
    assert peaks([2, 2, 2, 2]) == [False] * 4
    assert peaks([1, 1, 3]) == [False, False, True]  # 3 ≥ max(3, 3·1)


def test_sparkline_scales_to_the_max():
    assert sparkline([0, 0, 0]) == "▁▁▁"
    assert sparkline([1, 4, 8]) == "▂▅█"
    assert sparkline([]) == ""
