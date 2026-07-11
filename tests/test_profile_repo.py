from homyak.storage.postgres import NewsRepo


async def test_set_profile_seeds_affinity_and_versions(session_factory):
    repo = NewsRepo(session_factory)

    v1 = await repo.set_profile(
        "интересы про rust",
        [{"name": "rust", "polarity": "love"}, {"name": "crypto", "polarity": "mute"}],
    )
    assert v1 == 1

    version, desc, topics = await repo.get_active_profile()
    assert version == 1 and "rust" in desc
    assert len(topics) == 2

    affs = await repo.get_tag_affinities(["rust", "crypto"])
    assert affs["rust"] == 0.8  # love
    assert affs["crypto"] == -1.0  # mute

    # новая версия деактивирует прежнюю и переседит веса
    v2 = await repo.set_profile("новый", [{"name": "rust", "polarity": "like"}])
    assert v2 == 2
    version2, _, _ = await repo.get_active_profile()
    assert version2 == 2
    assert (await repo.get_tag_affinities(["rust"]))["rust"] == 0.5  # like


async def test_no_profile_returns_none(session_factory):
    repo = NewsRepo(session_factory)
    assert await repo.get_active_profile() is None
    assert await repo.get_source_affinity("rss", "hn") == 0.0
    assert await repo.get_taste_n_liked() == 0
