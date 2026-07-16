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


async def test_reapply_does_not_wipe_learned_affinity(session_factory):
    """Стена: декларация (слой 1) не затирает выученное (слой 2) при переприменении.

    Раньше seed шёл безусловным do_update — каждый apply сбрасывал накопленное 👍/👎 в
    полярность темы, то есть обучение стиралось при любой правке профиля.
    """
    repo = NewsRepo(session_factory)
    await repo.set_profile("it", "desc", [{"name": "rust", "polarity": "love"}])
    await repo.bump_tag_affinity("it", ["rust"], direction=1, lr=0.1)  # как будто пришёл 👍
    learned = (await repo.get_tag_affinities("it", ["rust"]))["rust"]
    assert learned != 0.8  # обучение сдвинуло вес с посевного

    await repo.set_profile("it", "desc правленый", [{"name": "rust", "polarity": "love"}])
    assert (await repo.get_tag_affinities("it", ["rust"]))["rust"] == learned


async def test_changed_polarity_overrides_learned(session_factory):
    """Обратная сторона: ты передумал — это приказ, он обязан подействовать."""
    repo = NewsRepo(session_factory)
    await repo.set_profile("it", "desc", [{"name": "rust", "polarity": "love"}])
    await repo.bump_tag_affinity("it", ["rust"], direction=1, lr=0.1)

    await repo.set_profile("it", "desc", [{"name": "rust", "polarity": "mute"}])
    assert (await repo.get_tag_affinities("it", ["rust"]))["rust"] == -1.0


async def test_orphan_seeds_are_swept_but_learned_survive(session_factory):
    """Тема ушла из декларации: посевной вес убираем, выученный — не наш, оставляем.

    Из-за отсутствия этой уборки строка `medical: -1` пережила снятие мьюта и продолжала
    штрафовать всю вертикаль.
    """
    repo = NewsRepo(session_factory)
    await repo.set_profile(
        "it",
        "desc",
        [
            {"name": "rust", "polarity": "love"},
            {"name": "crypto", "polarity": "mute"},  # уйдёт, обучения нет → сирота
            {"name": "ai", "polarity": "like"},  # уйдёт, но обучение было → выживет
        ],
    )
    await repo.bump_tag_affinity("it", ["ai"], direction=1, lr=0.1)
    learned_ai = (await repo.get_tag_affinities("it", ["ai"]))["ai"]

    await repo.set_profile("it", "desc", [{"name": "rust", "polarity": "love"}])
    affs = await repo.get_tag_affinities("it", ["rust", "crypto", "ai"])
    assert "crypto" not in affs  # посевная сирота выметена
    assert affs["ai"] == learned_ai  # выученное пережило исчезновение темы
    assert affs["rust"] == 0.8


async def test_mute_button_does_not_touch_declaration(session_factory):
    """🔇 пишет в свой слой и НЕ трогает профиль.

    Раньше mute_topic звал set_profile: кнопка переписывала декларацию, мьютя первый тег
    статьи. У медицинской статьи первый тег — `medical`, и одно нажатие выключило вертикаль.
    """
    repo = NewsRepo(session_factory)
    v1 = await repo.set_profile("medical", "мед desc", [{"name": "pharma", "polarity": "love"}])

    await repo.mute_topic("medical", "medical")
    version, desc, topics = await repo.get_active_profile("medical")
    assert version == v1  # версия НЕ дёрнулась
    assert desc == "мед desc"
    assert [t["name"] for t in topics] == ["pharma"]  # декларация нетронута
    assert await repo.get_muted_tags("medical") == {"medical"}

    await repo.unmute_topic("medical", "medical")  # повторное 🔇 = отмена
    assert await repo.get_muted_tags("medical") == set()
    assert (await repo.get_active_profile("medical"))[0] == v1
