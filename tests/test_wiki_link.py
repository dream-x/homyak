from homyak.core import wiki
from homyak.core.wiki_link import related_pages


def _wiki(tmp_path, monkeypatch, entities=(), concepts=()):
    monkeypatch.setattr(wiki.settings, "wiki_dir", str(tmp_path))
    for kind, names in (("entities", entities), ("concepts", concepts)):
        (tmp_path / kind).mkdir(parents=True, exist_ok=True)
        for n in names:
            (tmp_path / kind / f"{wiki.slugify(n)}.md").write_text(f"# {n}\n")


def test_tags_and_title_entities_match_wiki_pages(tmp_path, monkeypatch):
    _wiki(tmp_path, monkeypatch, entities=["Ollama", "Claude Code", "n8n"], concepts=["self-hosting", "rag"])
    slugs = [(p["kind"], p["slug"]) for p in related_pages(
        "Running Claude Code with Ollama locally", ["Self-Hosting", "LLMs", "ollama"], ttl=0, limit=5)]
    assert slugs == [("concepts", "self-hosting"), ("entities", "ollama"), ("entities", "claude-code")]
    assert related_pages("nothing here", ["rust"], ttl=0) == []
    # limit режет хвост, порядок — теги раньше заголовка
    assert [p["slug"] for p in related_pages("n8n and Ollama", ["self-hosting"], ttl=0, limit=2)] == [
        "self-hosting", "n8n",
    ]


def test_links_point_at_the_wiki_page(tmp_path, monkeypatch):
    _wiki(tmp_path, monkeypatch, entities=["n8n"])
    monkeypatch.setattr(wiki.settings, "public_base_url", "http://host:8000/")
    (page,) = related_pages("Automating with n8n", [], ttl=0)
    assert page["url"] == "http://host:8000/wiki?kind=entities&slug=n8n" and page["title"] == "n8n"


def test_empty_wiki_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki.settings, "wiki_dir", str(tmp_path / "none"))
    assert related_pages("Ollama", ["ollama"], ttl=0) == []
