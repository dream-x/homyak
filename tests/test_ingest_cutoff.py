from datetime import datetime, timedelta, timezone

from homyak.adapters.sources.rss import _effective_cutoff

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
HOUR = 3600
FLOOR = 24.0  # ingest_min_window_hours по умолчанию


def cut(cursor, interval, floor=FLOOR):
    return _effective_cutoff(cursor, interval, NOW, 1.5, floor)


def test_first_contact_limited_by_floor_not_by_tiny_interval():
    """Регресс: окно = interval*1.5 без пола убивало источники с частым поллингом."""
    # hn: интервал 10 мин → окно было 15 мин; теперь пол 24ч
    assert cut(None, 10 * 60) == NOW - timedelta(hours=24)


def test_long_interval_wins_over_floor():
    # лента на 24ч: interval*1.5 = 36ч > пола 24ч → берём большее окно
    assert cut(None, 24 * HOUR) == NOW - timedelta(hours=36)


def test_fresh_cursor_wins_over_window():
    """Обычный режим: курсор свежее горизонта — только новое с прошлого поллинга."""
    cur = (NOW - timedelta(minutes=10)).isoformat()
    assert cut(cur, 30 * 60) == NOW - timedelta(minutes=10)


def test_stale_cursor_clamped_to_window():
    """После долгого простоя не догоняем весь долг — только окно."""
    cur = (NOW - timedelta(days=10)).isoformat()
    assert cut(cur, 30 * 60) == NOW - timedelta(hours=24)


def test_ranked_feed_entries_survive_the_window():
    """hn/lobsters: pubDate = момент сабмита, на морду фида пост попадает через 0.5-3ч.
    Раньше окно 15 мин их не ловило физически — источник молча выдавал ноль."""
    cutoff = cut(None, 10 * 60)
    submitted_3h_ago = NOW - timedelta(hours=3)
    assert submitted_3h_ago > cutoff, "пост с фронта hn снова отсекается"


def test_daily_digest_and_date_only_feeds_survive():
    """habr_best — дайджест за сутки; huggingface/nature датируют записи 00:00."""
    cutoff = cut(None, 60 * 60)  # интервал 1ч → окно было 1.5ч
    midnight_today = NOW.replace(hour=0, minute=0)
    assert midnight_today > cutoff, "фид с датой 00:00 снова отсекается"


def test_naive_and_broken_cursor_fall_back_to_horizon():
    assert cut("не-дата", 30 * 60) == NOW - timedelta(hours=24)
    naive = datetime(2026, 7, 15, 11, 55).isoformat()  # без tz → трактуем как UTC
    assert cut(naive, 8 * HOUR) == datetime(2026, 7, 15, 11, 55, tzinfo=timezone.utc)
