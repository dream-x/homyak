"""Пределы мержа similarity_dedup: окно по времени и потолок размера кластера (чистая логика)."""

from datetime import datetime, timedelta, timezone

from homyak.adapters.analyzers.similarity_dedup import cluster_full, within_window

_NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def test_close_in_time_is_inside_the_window():
    assert within_window(_NOW, (_NOW - timedelta(hours=5)).isoformat(), 72)
    assert within_window(_NOW, _NOW + timedelta(hours=71), 72)


def test_old_story_is_outside_the_window():
    assert not within_window(_NOW, (_NOW - timedelta(days=14)).isoformat(), 72)
    assert not within_window(_NOW, _NOW + timedelta(hours=73), 72)


def test_missing_dates_or_disabled_window_do_not_block():
    assert within_window(None, _NOW.isoformat(), 72)
    assert within_window(_NOW, None, 72)
    assert within_window(_NOW, (_NOW - timedelta(days=30)).isoformat(), 0)
    assert within_window(_NOW, "not-a-date", 72)


def test_naive_timestamps_are_read_as_utc():
    naive = (_NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert within_window(_NOW, naive, 72)
    assert within_window(naive, _NOW.isoformat(), 72)


def test_cluster_cap():
    assert not cluster_full(59, 60)
    assert cluster_full(60, 60)
    assert cluster_full(926, 60)
    assert not cluster_full(926, 0)  # 0 = проверка выключена
