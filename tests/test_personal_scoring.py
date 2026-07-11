from homyak.core.scoring import PersonalWeights, cosine, personal_score

W = PersonalWeights(llm=0.5, taste=0.2, tag=0.15, source=0.1, fresh=0.05)


def _ps(**kw):
    base = dict(
        llm_relevance=0.0,
        taste_cos=0.0,
        tag_aff=0.0,
        source_aff=0.0,
        freshness_val=0.0,
        n_liked=0,
        weights=W,
        taste_ramp=20,
    )
    base.update(kw)
    return personal_score(**base)


def test_cold_start_taste_zero_without_likes():
    # n_liked=0 → taste-компонента = 0, даже если cosine=1
    assert abs(_ps(llm_relevance=1.0, taste_cos=1.0, n_liked=0) - 0.5) < 1e-9


def test_taste_ramps_up_with_likes():
    assert abs(_ps(taste_cos=1.0, n_liked=10) - 0.2 * 0.5) < 1e-9  # ramp 0.5
    assert abs(_ps(taste_cos=1.0, n_liked=40) - 0.2) < 1e-9  # ramp cap 1.0


def test_negative_tag_affinity_lowers_score():
    assert _ps(llm_relevance=0.5, tag_aff=1.0) > _ps(llm_relevance=0.5, tag_aff=-1.0)


def test_llm_relevance_dominates():
    assert _ps(llm_relevance=1.0) > _ps(llm_relevance=0.0, tag_aff=1.0, source_aff=1.0)


def test_cosine_basic():
    assert abs(cosine([1, 0, 0], [1, 0, 0]) - 1.0) < 1e-9
    assert abs(cosine([1, 0], [0, 1])) < 1e-9
    assert cosine(None, [1.0]) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0
