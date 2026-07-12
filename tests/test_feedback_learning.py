from homyak.core.interfaces import NewsItemDTO
from homyak.storage.postgres import NewsRepo


async def test_record_feedback_toggle(session_factory):
    repo = NewsRepo(session_factory)
    id_, _ = await repo.upsert_item(NewsItemDTO(source_type="rss", source_id="a", title="t"))
    assert await repo.record_feedback(id_, "up") == "added"
    assert await repo.record_feedback(id_, "up") == "removed"  # повторный клик — toggle off
    assert await repo.record_feedback(id_, "up") == "added"


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


async def test_mute_topic_bumps_profile_version(session_factory):
    repo = NewsRepo(session_factory)
    v1 = await repo.set_profile("it", "интересы", [{"name": "rust", "polarity": "love"}])
    v2 = await repo.mute_topic("it", "crypto")
    assert v2 == v1 + 1
    _, _, topics = await repo.get_active_profile("it")
    assert any(t["name"] == "crypto" and t["polarity"] == "mute" for t in topics)
