from homyak.core.trends import direction, growth, strength


def test_growth():
    assert growth(20, 10) == 1.0          # удвоение
    assert growth(10, 20) == -0.5         # спад
    assert growth(5, 5) == 0.0            # ровно
    assert growth(5, 0) == 2.0            # новая тема — умеренный буст, не бесконечность
    assert growth(0, 0) == 0.0


def test_direction():
    assert direction(20, 10) == "↑"       # рост
    assert direction(11, 10) == "→"       # почти ровно (< 25%)
    assert direction(5, 10) == "↓"        # спад


def test_strength_orders_by_volume_growth_relevance():
    # больше объём при прочих равных → сильнее
    assert strength(20, 10, 0.5) > strength(10, 5, 0.5)
    # выше релевантность → сильнее
    assert strength(10, 10, 0.9) > strength(10, 10, 0.2)
    # рост усиливает; спад не опускает ниже базового объёма (cap снизу 0)
    assert strength(10, 5, 0.5) > strength(10, 20, 0.5)


def test_strength_growth_capped():
    # взрывной рост не даёт бесконечную силу (cap 3x)
    huge = strength(100, 1, 0.5)
    capped = strength(100, 25, 0.5)  # рост 3x — уже на потолке
    assert huge == capped
