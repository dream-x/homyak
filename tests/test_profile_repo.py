import pytest

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


# --- канонические группы: исторические ключи (golang) и новые (go) — одна тема ---------------


async def _historical_state(session_factory):
    """Как в проде до канона: активный профиль golang/love и ВЫУЧЕННАЯ строка golang."""
    from homyak.core.models import Profile, TagAffinity

    async with session_factory() as s:
        s.add(
            Profile(
                vertical="it", version=1, description="old",
                topics=[{"name": "golang", "polarity": "love"}], active=True,
            )
        )
        s.add(TagAffinity(vertical="it", tag="golang", weight=0.2, n_pos=9, n_neg=0))
        await s.commit()


async def test_reapply_with_a_historical_alias_does_not_seed_a_second_row(session_factory):
    """Переприменение (та же полярность) не сеет `go` рядом с выученным `golang`: чтение
    усредняет группу, и посев сдвигал бы выученный вес."""
    repo = NewsRepo(session_factory)
    await _historical_state(session_factory)

    await repo.set_profile("it", "desc правленый", [{"name": "golang", "polarity": "love"}])

    assert (await repo.get_tag_affinities("it", ["go"]))["go"] == pytest.approx(0.2)
    assert await _rows(session_factory) == {("golang", 0.2, 9)}


async def test_changed_polarity_overrides_the_whole_canonical_group(session_factory):
    """Смена полярности — приказ всей группе: исторический golang тоже получает новый вес,
    свидетельства (n_pos) остаются."""
    repo = NewsRepo(session_factory)
    await _historical_state(session_factory)

    await repo.set_profile("it", "desc", [{"name": "go", "polarity": "dislike"}])

    assert (await repo.get_tag_affinities("it", ["go"]))["go"] == pytest.approx(-0.5)
    assert await _rows(session_factory) == {("golang", -0.5, 9)}


async def test_orphan_sweep_recognises_historical_spellings(session_factory):
    """Посев `self-hosted` (нетронутый) уходит из декларации под именем selfhosted — выметается."""
    from homyak.core.models import TagAffinity

    repo = NewsRepo(session_factory)
    async with session_factory() as s:
        s.add(TagAffinity(vertical="it", tag="self-hosted", weight=0.8, n_pos=0, n_neg=0))
        await s.commit()
    await repo.set_profile("it", "desc", [{"name": "rust", "polarity": "like"}])
    assert await _rows(session_factory) == {("rust", 0.5, 0)}


async def test_unmute_clears_every_spelling_of_the_topic(session_factory):
    repo = NewsRepo(session_factory)
    from homyak.core.models import MutedTag

    async with session_factory() as s:  # исторический ключ, как писала старая кнопка
        s.add(MutedTag(vertical="it", tag="golang"))
        await s.commit()
    assert await repo.get_muted_tags("it") == {"go"}
    assert await repo.unmute_topic("it", "go") == 1
    assert await repo.get_muted_tags("it") == set()

    await repo.mute_topic("it", "go")  # новая кнопка пишет канон
    assert await repo.unmute_topic("it", "golang") == 1
    assert await repo.get_muted_tags("it") == set()

    async with session_factory() as s:  # оба ключа сразу
        s.add_all([MutedTag(vertical="it", tag="golang"), MutedTag(vertical="it", tag="go")])
        await s.commit()
    assert await repo.unmute_topic("it", "Go-Lang") == 2
    assert await repo.get_muted_tags("it") == set()
    assert await repo.unmute_topic("it", "go") == 0


async def test_two_spellings_in_one_declaration_are_one_topic(session_factory):
    """observability + monitoring в одной декларации (живой файл): один посев, не дубль PK;
    переприменение с правкой description не трогает выученный вес группы."""
    from homyak.core.models import TagAffinity
    from sqlalchemy import update

    repo = NewsRepo(session_factory)
    decl = [
        {"name": "observability", "polarity": "love"},
        {"name": "monitoring", "polarity": "like"},
        {"name": "self-hosted", "polarity": "love"},
        {"name": "self-hosting", "polarity": "love"},
    ]
    await repo.set_profile("it", "v1", decl)
    assert await _rows(session_factory) == {("observability", 0.8, 0), ("selfhosted", 0.8, 0)}

    async with session_factory() as s:  # learner накопил свидетельства и сдвинул вес
        await s.execute(
            update(TagAffinity)
            .where(TagAffinity.vertical == "it", TagAffinity.tag == "observability")
            .values(weight=-0.2, n_pos=6)
        )
        await s.commit()
    await repo.set_profile("it", "v2 — правка описания", decl)
    assert await _rows(session_factory) == {("observability", -0.2, 6), ("selfhosted", 0.8, 0)}

    decl[0]["polarity"] = "dislike"  # ты передумал — приказ группе, свидетельства остаются
    await repo.set_profile("it", "v3", decl)
    assert await _rows(session_factory) == {("observability", -0.5, 6), ("selfhosted", 0.8, 0)}


async def _rows(session_factory) -> set[tuple[str, float, int]]:
    from sqlalchemy import select

    from homyak.core.models import TagAffinity

    async with session_factory() as s:
        rows = (await s.execute(select(TagAffinity.tag, TagAffinity.weight, TagAffinity.n_pos))).all()
    return {(r[0], round(float(r[1]), 3), int(r[2])) for r in rows}
