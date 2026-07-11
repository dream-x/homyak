from homyak.core.scoring import centroid_add, centroid_remove


def test_centroid_add_is_running_mean():
    c, n = centroid_add(None, 0, [1.0, 0.0])
    assert c == [1.0, 0.0] and n == 1
    c, n = centroid_add(c, n, [0.0, 1.0])
    assert c == [0.5, 0.5] and n == 2


def test_centroid_remove_reverses_add():
    c, n = centroid_add(None, 0, [1.0, 0.0])
    c, n = centroid_add(c, n, [0.0, 1.0])
    c, n = centroid_add(c, n, [1.0, 1.0])  # n=3
    c, n = centroid_remove(c, n, [1.0, 1.0])  # откат последнего
    assert n == 2
    assert abs(c[0] - 0.5) < 1e-9 and abs(c[1] - 0.5) < 1e-9


def test_centroid_remove_last_gives_empty():
    c, n = centroid_add(None, 0, [1.0, 2.0])
    c, n = centroid_remove(c, n, [1.0, 2.0])
    assert c is None and n == 0
