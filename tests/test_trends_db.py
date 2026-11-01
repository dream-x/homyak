"""Тренды и слой совместимости канона — против живой БД (homyak_test).

Канон применяется В SQL до distinct по историям: одна история с двумя написаниями — одна история;
подборка находит те же написания, что посчитал счётчик; affinity и мьюты с историческими ключами
(golang, self-hosted, «ai agents») продолжают действовать на канонические теги (go, selfhosted,
ai-agents).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update

from homyak.core.interfaces import NewsItemDTO
from homyak.core.models import Cluster, MutedTag, NewsItem, TagAffinity
from homyak.core.tags import ALIAS_CTE, alias_params, canon_lateral, normalize_tag
from homyak.core.trends import trend_items, trend_report
from homyak.storage.postgres import NewsRepo

_NOW = datetime.now(timezone.utc)


async def _story(session_factory, repo, sid: str, tags_per_item: list[list[str]]) -> list[int]:
    """Одна история: items с данными тегами (несколько → один кластер), прошедшие ранкер."""
    ids = []
    for i, tags in enumerate(tags_per_item):
        iid, _ = await repo.upsert_item(
            NewsItemDTO(
                source_type="rss",
                source_id=f"{sid}-{i}",
                url=f"https://x.test/{sid}/{i}",
                published_at=_NOW - timedelta(hours=1),
            )
        )
        ids.append(iid)
    async with session_factory() as s:
        c = Cluster(representative_id=ids[0], size=len(ids))
        s.add(c)
        await s.flush()
        for iid, tags in zip(ids, tags_per_item):
            await s.execute(
                update(NewsItem)
                .where(NewsItem.id == iid)
                .values(
                    cluster_id=c.id, tags=tags, vertical="it", processed_at=_NOW,
                    personal_score=0.7, title=f"story {sid}",
                )
            )
        await s.commit()
    return ids


async def test_sql_canon_matches_python(session_factory):
    raw = [
        "  AI Agents ", "ai_agents", "#AI-agents", "ai--agents-", "c++", "m&a", "LLMs",
        "large language models", "Federal Reserve", "self hosted", "Rust-lang", "x" * 41,
        "   ", "#", "ИИ-Агенты", "golang", "k8s", "open-source", "a/b testing",
        "ai\u00a0agents", "\t#go", "\u3000Rust\u3000", "\u00a0", "ai\u2003agents",
    ]
    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    f"with {ALIAS_CTE} select t raw, c.canon"
                    f" from unnest(cast(:raw as text[])) t {canon_lateral('t')}"
                ),
                {"raw": raw, **alias_params()},
            )
        ).all()
    got = {r.raw: r.canon for r in rows}
    assert got == {r: normalize_tag(r) for r in raw}


async def test_a_story_with_several_spellings_is_one_story(session_factory):
    repo = NewsRepo(session_factory)
    await _story(session_factory, repo, "s1", [["llm"], ["llms"]])  # кластер: два написания
    await _story(session_factory, repo, "s2", [["LLMs", "large-language-models"]])  # оба на одной
    await _story(session_factory, repo, "s3", [["llm"]])
    await _story(session_factory, repo, "s4", [["ai_agents"]])
    await _story(session_factory, repo, "s5", [["ai agents"]])
    await _story(session_factory, repo, "s6", [["AI-Agents", "rust"]])

    trends, fading = await trend_report("day", sf=session_factory)
    by_tag = {t["tag"]: t for t in trends}
    assert by_tag["llm"]["count"] == 3  # не 5
    assert by_tag["ai-agents"]["count"] == 3
    assert "rust" not in by_tag  # ниже min_count дня (3)
    assert by_tag["llm"]["avg_score"] == 0.7
    assert fading == []


async def test_trend_items_find_every_spelling_the_counter_saw(session_factory):
    repo = NewsRepo(session_factory)
    await _story(session_factory, repo, "s1", [["llm"], ["llms"]])
    await _story(session_factory, repo, "s2", [["large language models"]])
    await _story(session_factory, repo, "s4", [["ai_agents"]])
    await _story(session_factory, repo, "s5", [["ai agents"]])

    llm = await trend_items("LLMs", "day", sf=session_factory)
    assert sorted(i["title"] for i in llm) == ["story s1", "story s2"]  # кластер схлопнут
    agents = await trend_items("ai-agents", "day", sf=session_factory)
    assert sorted(i["title"] for i in agents) == ["story s4", "story s5"]


async def test_affinity_lookup_folds_historical_keys(session_factory):
    repo = NewsRepo(session_factory)
    async with session_factory() as s:
        s.add_all(
            [
                TagAffinity(vertical="it", tag="golang", weight=0.6, n_pos=3, n_neg=0),
                TagAffinity(vertical="it", tag="go", weight=0.0, n_pos=1, n_neg=0),
                TagAffinity(vertical="it", tag="self-hosted", weight=0.9, n_pos=0, n_neg=0),
                TagAffinity(vertical="it", tag="ai agents", weight=-0.4, n_pos=0, n_neg=2),
                TagAffinity(vertical="business", tag="go", weight=-1.0, n_pos=0, n_neg=5),
            ]
        )
        await s.commit()

    aff = await repo.get_tag_affinities("it", ["go", "selfhosted", "AI Agents", "rust"])
    # golang (n=4) и go (n=2): (0.6·4 + 0·2) / 6
    assert aff == pytest.approx({"go": 0.4, "selfhosted": 0.9, "ai-agents": -0.4})


async def test_muted_tags_and_profile_mutes_come_back_canonical(session_factory):
    repo = NewsRepo(session_factory)
    async with session_factory() as s:
        s.add(MutedTag(vertical="it", tag="golang"))
        await s.commit()
    assert await repo.get_muted_tags("it") == {"go"}

    await repo.set_profile(
        "it", "desc", [{"name": "Self-Hosted", "polarity": "love"}, {"name": "k8s", "polarity": "mute"}]
    )
    assert await repo.get_tag_affinities("it", ["selfhosted", "kubernetes"]) == pytest.approx(
        {"selfhosted": 0.8, "kubernetes": -1.0}  # love = 0.8, mute = -1.0
    )
