from datetime import datetime, timedelta, timezone

from homyak.adapters.sources.rss import _effective_cutoff

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
HOUR = 3600


def test_first_contact_starts_from_window_not_history():
    # курсора нет → берём только окно (30мин * 1.5 = 45мин), а не всю выдачу фида
    got = _effective_cutoff(None, 30 * 60, NOW, 1.5)
    assert got == NOW - timedelta(minutes=45)


def test_fresh_cursor_wins():
    # курсор свежее горизонта → работает курсор (обычный режим)
    cur = (NOW - timedelta(minutes=10)).isoformat()
    assert _effective_cutoff(cur, 8 * HOUR, NOW, 1.5) == NOW - timedelta(minutes=10)


def test_stale_cursor_after_downtime_does_not_chase_debt():
    # стек лежал 12ч → курсор протух; не догоняем долги, стартуем от горизонта (45мин)
    cur = (NOW - timedelta(hours=12)).isoformat()
    got = _effective_cutoff(cur, 30 * 60, NOW, 1.5)
    assert got == NOW - timedelta(minutes=45)


def test_window_scales_with_interval():
    # лента на 8ч → окно 12ч (иначе теряли бы то, что вышло между поллингами)
    assert _effective_cutoff(None, 8 * HOUR, NOW, 1.5) == NOW - timedelta(hours=12)


def test_naive_and_broken_cursor_fall_back_to_horizon():
    assert _effective_cutoff("не-дата", 30 * 60, NOW, 1.5) == NOW - timedelta(minutes=45)
    naive = datetime(2026, 7, 15, 11, 55).isoformat()  # без tz → трактуем как UTC
    assert _effective_cutoff(naive, 8 * HOUR, NOW, 1.5) == datetime(
        2026, 7, 15, 11, 55, tzinfo=timezone.utc
    )
