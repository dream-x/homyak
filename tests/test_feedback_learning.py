from homyak.core.interfaces import NewsItemDTO
from homyak.storage.postgres import NewsRepo


async def test_record_feedback_toggle(session_factory):
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(NewsItemDTO(source_type="rss", source_id="a", title="t"))
    assert (await repo.record_feedback(id_, "up"))[0] == "added"
    assert (await repo.record_feedback(id_, "up"))[0] == "removed"  # повторный клик — toggle off
    assert (await repo.record_feedback(id_, "up"))[0] == "added"


async def test_bump_tag_affinity_directions_and_clip(session_factory):
    repo = NewsRepo(session_factory)
    await repo.bump_tag_affinity("it", ["rust"], +1, 0.5)  # 0 + 0.5*(1-0) = 0.5
    assert (await repo.get_tag_affinities("it", ["rust"]))["rust"] == 0.5
    await repo.bump_tag_affinity("it", ["rust"], +1, 0.5)  # 0.5 + 0.5*(1-0.5) = 0.75
    assert abs((await repo.get_tag_affinities("it", ["rust"]))["rust"] - 0.75) < 1e-9
    await repo.bump_tag_affinity("it", ["rust"], -1, 0.5)  # 0.75 + 0.5*(-1-0.75) = -0.125
    assert abs((await repo.get_tag_affinities("it", ["rust"]))["rust"] + 0.125) < 1e-9
    # другая вертикаль не затронута
    assert await repo.get_tag_affinities("business", ["rust"]) == {}


async def test_source_affinity_roundtrip(session_factory):
    repo = NewsRepo(session_factory)
    assert await repo.get_source_affinity("it", "rss", "hn") == 0.0
    await repo.bump_source_affinity("it", "rss", "hn", +1, 0.5)
    assert await repo.get_source_affinity("it", "rss", "hn") == 0.5


async def test_mute_topic_lands_in_learned_layer_not_declaration(session_factory):
    """Инверсия прежнего поведения: раньше 🔇 дёргал версию профиля — теперь не смеет.

    Тест назывался test_mute_topic_bumps_profile_version и закреплял именно тот механизм,
    который выкосил 21% медицинской вертикали: кнопка дописывала mute в profile.topics,
    то есть переписывала декларацию пользователя. Мьют — это выученное, ему место в muted_tags.
    """
    repo = NewsRepo(session_factory)
    v1 = await repo.set_profile("it", "интересы", [{"name": "rust", "polarity": "love"}])
    await repo.mute_topic("it", "crypto")

    version, _, topics = await repo.get_active_profile("it")
    assert version == v1  # версия профиля не дёрнулась
    assert [t["name"] for t in topics] == ["rust"]  # в декларацию crypto не просочился
    assert await repo.get_muted_tags("it") == {"crypto"}


async def test_mute_several_tags_on_one_item(session_factory):
    """Мьют двухшаговый — на одной статье можно выбрать несколько тегов, и toggle снимает свой.

    До UNIQUE(item, signal, topic) второй мьют СНИМАЛ первый: тема в ключ не входила, и
    пользователь, ткнув «🔇 sports», получал «🔇 «politics» снято», а sports не мьютился вовсе.
    """
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(NewsItemDTO(source_type="rss", source_id="m1", title="t"))
    assert await repo.record_feedback(id_, "mute_topic", "politics") == ("added", "politics")
    assert await repo.record_feedback(id_, "mute_topic", "sports") == ("added", "sports")
    # снимаем только politics — sports обязан остаться
    assert await repo.record_feedback(id_, "mute_topic", "politics") == ("removed", "politics")
    assert await repo.record_feedback(id_, "mute_topic", "sports") == ("removed", "sports")


async def test_null_topic_still_toggles(session_factory):
    """NULLS NOT DISTINCT: у 👍/👎/⭐ topic=NULL, и toggle обязан продолжать работать."""
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(NewsItemDTO(source_type="rss", source_id="m2", title="t"))
    assert (await repo.record_feedback(id_, "up"))[0] == "added"
    assert (await repo.record_feedback(id_, "up"))[0] == "removed"
    assert (await repo.record_feedback(id_, "up"))[0] == "added"
