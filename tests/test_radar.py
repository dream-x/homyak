from datetime import date

from homyak.core.radar import repo_key, velocity


def test_repo_key_normalises_github_urls():
    assert repo_key("https://github.com/Anthropics/Claude-Code/tree/main/src") == "anthropics/claude-code"
    assert repo_key("https://www.github.com/a/b.git") == "a/b"
    assert repo_key("https://github.com/trending/python") is None
    assert repo_key("https://gitlab.com/a/b") is None
    assert repo_key(None) is None


def test_velocity_uses_first_and_last_snapshot():
    v = velocity([(date(2026, 10, 25), 100), (date(2026, 10, 31), 400), (date(2026, 10, 28), 200)])
    assert v == {"stars": 400, "gained": 300, "per_day": 50.0, "span_days": 6}
    assert velocity([(date(2026, 10, 25), 100)]) is None
    assert velocity([]) is None
    same = velocity([(date(2026, 10, 25), 5), (date(2026, 10, 25), 9)])  # тот же день → span 1
    assert same["span_days"] == 1 and same["per_day"] == 4.0
