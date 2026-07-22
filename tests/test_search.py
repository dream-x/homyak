from homyak.core.search import reciprocal_rank_fusion


def test_rrf_empty():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_rrf_single_list_preserves_order():
    assert reciprocal_rank_fusion([[3, 1, 2]]) == [3, 1, 2]


def test_rrf_item_in_both_lists_wins():
    # 7 стоит в обоих списках → его сумма очков выше любого одиночки
    sem = [7, 1, 2, 3]
    lex = [4, 5, 7, 6]
    fused = reciprocal_rank_fusion([sem, lex])
    assert fused[0] == 7
    assert set(fused) == {1, 2, 3, 4, 5, 6, 7}


def test_rrf_higher_rank_beats_lower():
    # оба уникальны и в одном списке — верхний по рангу впереди
    assert reciprocal_rank_fusion([[10, 20]]) == [10, 20]


def test_rrf_stable_on_ties():
    # разные списки, нет пересечений: очки равны у одинаковых позиций → порядок первого появления
    fused = reciprocal_rank_fusion([[1], [2]])
    assert fused == [1, 2]


def test_rrf_symmetric_reversed_lists():
    # sem и lex — зеркальны: каждый край получает по одному топ-1 попаданию и обходит
    # серединку (у неё два средних ранга). Классическое свойство RRF.
    fused = reciprocal_rank_fusion([[1, 2, 3], [3, 2, 1]])
    assert set(fused) == {1, 2, 3}
    assert fused[-1] == 2  # двойная середина — в самом низу
