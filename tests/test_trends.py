from homyak.core.trends import direction, growth, strength


def test_growth():
    assert growth(20, 10) == 1.0  # удвоение
    assert growth(10, 20) == -0.5  # спад
    assert growth(5, 5) == 0.0  # ровно
    assert growth(5, 0) == 2.0  # новая тема — умеренный буст, не бесконечность
    assert growth(0, 0) == 0.0


def test_direction():
    assert direction(20, 10) == "↑"  # рост
    assert direction(11, 10) == "→"  # почти ровно (< 25%)
    assert direction(5, 10) == "↓"  # спад


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


def test_share_growth_ignores_a_collapse_of_the_whole_feed():
    from homyak.core.trends import share_growth

    # лента просела вдвое (прокси лёг): тема упала с 20 до 10 историй, но её доля та же → рост 0
    assert share_growth(10, 100, 20, 200) == 0.0
    # тот же объём при вдвое меньшей ленте → доля удвоилась
    assert share_growth(10, 100, 10, 200) == 1.0
    # без итогов — обычный growth
    assert share_growth(20, 0, 10, 0) == 1.0


def test_merge_canonical_folds_synonyms_and_drops_broad_tags():
    from homyak.core.trends import merge_canonical

    merged = merge_canonical(
        [("llm", 10, 4, 0.8), ("LLMs", 5, 1, 0.2), ("ai", 50, 50, 0.5), ("rust", 3, 0, None)]
    )
    assert set(merged) == {"llm", "rust"}
    assert merged["llm"]["count"] == 15 and merged["llm"]["prev"] == 5
    assert merged["llm"]["avg_score"] == 0.6  # (0.8·10 + 0.2·5) / 15
    assert merged["rust"]["avg_score"] is None


def test_rank_and_fading_selection():
    from homyak.core.trends import merge_canonical, rank_trends, select_fading

    merged = merge_canonical(
        [
            ("ai-agents", 30, 10, 0.7),  # разгон
            ("crypto", 4, 20, 0.5),  # затихло: было 20, стало 4
            ("earnings", 2, 2, 0.5),  # ниже min_count
            ("rust", 6, 6, 0.9),  # ровно
        ]
    )
    top = rank_trends(merged, total=100, prev_total=100, min_count=3, limit=8)
    assert [t["tag"] for t in top][:1] == ["ai-agents"]
    assert {t["tag"] for t in top} == {"ai-agents", "crypto", "rust"}
    assert next(t for t in top if t["tag"] == "ai-agents")["direction"] == "↑"
    assert next(t for t in top if t["tag"] == "crypto")["growth"] == -0.8

    quiet = select_fading(merged, total=100, prev_total=100, min_prev=3, limit=5)
    assert [t["tag"] for t in quiet] == ["crypto"]
