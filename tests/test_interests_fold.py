"""fold_topics: декларация сворачивается по канону, первое написание задаёт полярность."""

from homyak.core.interests import Topic, fold_topics


def test_synonyms_fold_into_one_topic_first_polarity_wins():
    folded, warnings = fold_topics(
        [
            {"name": "observability", "polarity": "love"},
            {"name": "monitoring", "polarity": "like"},
            {"name": "self-hosted", "polarity": "love"},
            {"name": "self-hosting", "polarity": "love"},
            {"name": "Go-Lang"},
            {"name": ""},
        ]
    )
    assert folded == {"observability": "love", "selfhosted": "love", "go": "like"}
    assert len(warnings) == 1
    assert "observability и monitoring" in warnings[0] and "love / like" in warnings[0]


def test_models_and_dicts_fold_the_same():
    models = [Topic(name="golang", polarity="love"), Topic(name="go", polarity="meh")]
    dicts = [t.model_dump() for t in models]
    assert fold_topics(models) == fold_topics(dicts) == ({"go": "love"}, fold_topics(dicts)[1])
