from types import SimpleNamespace

import pytest

from homyak.core import wiki, wiki_ingest, wiki_query
from homyak.core.config import settings
from homyak.core.wiki import slugify


@pytest.fixture(autouse=True)
def wiki_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path))
    wiki.ensure_dirs()
    return tmp_path


def test_slugify():
    assert slugify("Continuous Batching") == "continuous-batching"
    assert slugify("  vLLM / PagedAttention!! ") == "vllm-pagedattention"
    assert slugify("Диффузия вины") == "диффузия-вины"
    assert slugify("") == "untitled"
    assert slugify("---") == "untitled"


class _FakeLLM:
    def __init__(self, extract):
        self._extract = extract

    async def chat_json(self, system, user):
        return self._extract

    async def chat_text(self, system, user, think=None):
        # отвечает, процитировав первую страницу из контекста
        return "Ответ на основе вики про RAG (concepts/rag)."


def _item(**kw):
    base = dict(
        id=101, title="Retrieval-Augmented Generation on Rust",
        summary="RAG pipeline in Rust with vector search.", text=None,
        url="https://github.com/x/rag-rs", feed_name="gh_search_rust",
        source_type="rss", tags=["rust", "rag"],
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_ingest_creates_source_concept_entity_and_log():
    llm = _FakeLLM(
        {
            "concepts": [{"name": "RAG", "note": "retrieval-augmented generation"}],
            "entities": [{"name": "Rust", "note": "systems language"}],
        }
    )
    res = await wiki_ingest.ingest_item(_item(), llm)
    assert res["concepts"] == ["rag"]
    assert res["entities"] == ["rust"]
    # source-страница
    src = wiki.read_page("sources", "101-retrieval-augmented-generation-on-rust")
    assert src and "github.com/x/rag-rs" in src
    # концепт с датированным буллетом и wikilink на источник
    rag = wiki.read_page("concepts", "rag")
    assert rag and "[[sources/101-retrieval-augmented-generation-on-rust]]" in rag
    assert "## Упоминания" in rag
    # log дописан
    assert "ingest |" in (wiki.wiki_root() / "log.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_ingest_idempotent_no_duplicate_mentions():
    llm = _FakeLLM({"concepts": [{"name": "RAG", "note": "x"}], "entities": []})
    await wiki_ingest.ingest_item(_item(), llm)
    await wiki_ingest.ingest_item(_item(), llm)  # тот же источник второй раз
    rag = wiki.read_page("concepts", "rag")
    assert rag.count("[[sources/101-retrieval-augmented-generation-on-rust]]") == 1


@pytest.mark.asyncio
async def test_ingest_survives_llm_failure():
    class Boom:
        async def chat_json(self, s, u):
            raise RuntimeError("down")

    res = await wiki_ingest.ingest_item(_item(), Boom())
    assert res["concepts"] == [] and res["entities"] == []
    # source и log всё равно записаны
    assert wiki.read_page("sources", "101-retrieval-augmented-generation-on-rust")


@pytest.mark.asyncio
async def test_query_finds_ingested_page():
    llm = _FakeLLM({"concepts": [{"name": "RAG", "note": "retrieval-augmented generation"}], "entities": []})
    await wiki_ingest.ingest_item(_item(), llm)
    res = await wiki_query.answer_from_wiki("что такое RAG", max_pages=5)
    assert res is not None
    assert res["answer"]
    assert any("concepts/rag" in s["title"] for s in res["sources"])


@pytest.mark.asyncio
async def test_query_empty_wiki_returns_none():
    assert await wiki_query.answer_from_wiki("что угодно") is None


def test_run_lint_flags_weak_pages():
    wiki.write_page("concepts", "lonely", "# Lonely\n\nx\n\n## Упоминания\n- [2026-01-01] a — [[sources/1-a]]")
    out = wiki.run_lint()
    assert out["pages"]["concepts"] >= 1
    assert out["weak"] >= 1
    assert "lint" in (wiki.wiki_root() / "lint.md").read_text(encoding="utf-8")
