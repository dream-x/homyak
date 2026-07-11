from datetime import datetime, timedelta, timezone

from homyak.core.scoring import base_score, freshness

_NOW = datetime(2024, 1, 10, tzinfo=timezone.utc)


def test_freshness_decays_to_e_inverse_at_tau():
    assert abs(freshness(_NOW, _NOW) - 1.0) < 1e-9
    at_tau = freshness(_NOW - timedelta(hours=48), _NOW)
    assert 0.36 < at_tau < 0.38  # e^-1
    # без даты — штраф как за сутки
    assert freshness(None, _NOW) < 1.0


def test_base_score_rewards_freshness_cluster_and_raw():
    s = base_score(_NOW, 0.0, 1, _NOW)
    assert base_score(_NOW, 0.0, 10, _NOW) > s  # больше кластер → выше
    assert base_score(_NOW, 1.0, 1, _NOW) > s  # внешняя оценка → выше
    assert base_score(_NOW - timedelta(hours=96), 0.0, 1, _NOW) < s  # старее → ниже


def test_base_score_handles_none_cluster_size():
    assert base_score(_NOW, None, 0, _NOW) > 0  # size<1 нормализуется к 1
