from homyak.storage.postgres import NewsRepo


async def test_set_profile_seeds_affinity_and_versions(session_factory):
    repo = NewsRepo(session_factory)

    v1 = await repo.set_profile(
        "it",
        "интересы про rust",
        [{"name": "rust", "polarity": "love"}, {"name": "crypto", "polarity": "mute"}],
    )
    assert v1 == 1

    version, desc, topics = await repo.get_active_profile("it")
    assert version == 1 and "rust" in desc
    assert len(topics) == 2

    affs = await repo.get_tag_affinities("it", ["rust", "crypto"])
    assert affs["rust"] == 0.8  # love
    assert affs["crypto"] == -1.0  # mute

    v2 = await repo.set_profile("it", "новый", [{"name": "rust", "polarity": "like"}])
    assert v2 == 2
    version2, _, _ = await repo.get_active_profile("it")
    assert version2 == 2
    assert (await repo.get_tag_affinities("it", ["rust"]))["rust"] == 0.5  # like


async def test_profiles_are_independent_per_vertical(session_factory):
    repo = NewsRepo(session_factory)
    await repo.set_profile("it", "it desc", [{"name": "rust", "polarity": "love"}])
    await repo.set_profile("business", "biz desc", [{"name": "rust", "polarity": "mute"}])
    # один и тот же тег — разный вес в разных вертикалях
    assert (await repo.get_tag_affinities("it", ["rust"]))["rust"] == 0.8
    assert (await repo.get_tag_affinities("business", ["rust"]))["rust"] == -1.0
    assert (await repo.get_active_profile("it"))[1] == "it desc"
    assert (await repo.get_active_profile("business"))[1] == "biz desc"


async def test_no_profile_returns_none(session_factory):
    repo = NewsRepo(session_factory)
    assert await repo.get_active_profile("it") is None
    assert await repo.get_source_affinity("it", "rss", "hn") == 0.0
    assert await repo.get_taste_n_liked("it") == 0
