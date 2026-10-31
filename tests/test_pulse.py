from homyak.core.pulse import breadth_rank, watch_delta


def test_breadth_beats_volume_beats_score():
    stories = [
        {"id": 1, "feeds": 1, "items": 10, "score": 0.9},
        {"id": 2, "feeds": 4, "items": 4, "score": 0.5},
        {"id": 3, "feeds": 4, "items": 6, "score": 0.4},
        {"id": 4, "feeds": 2, "items": 2, "score": None},
    ]
    ranked = breadth_rank(stories, limit=3)
    assert [s["id"] for s in ranked] == [3, 2, 4]
    assert [s["wide"] for s in ranked] == [True, True, False]


def test_watch_delta_against_the_typical_window():
    d = watch_delta("Iran", 6, 1.5)
    assert d["growth"] == 3.0 and d["direction"] == "↑" and d["base"] == 1.5
    assert watch_delta("Oil", 0, 2.0)["direction"] == "↓"
    assert watch_delta("New", 2, 0.0)["growth"] == 2.0  # новая тема: умеренный буст, не ∞
    assert watch_delta("Flat", 3, 3.0)["direction"] == "→"
