from homyak.core import wiki
from homyak.core.wiki_link import related_pages


def _wiki(tmp_path, monkeypatch, entities=(), concepts=()):
    """Страницы: имя или (имя, число источников) — датированных пунктов, как пишет ingest."""
    monkeypatch.setattr(wiki.settings, "wiki_dir", str(tmp_path))
    for kind, names in (("entities", entities), ("concepts", concepts)):
        (tmp_path / kind).mkdir(parents=True, exist_ok=True)
        for n in names:
            name, cnt = n if isinstance(n, tuple) else (n, 1)
            bullets = "".join(f"- [2026-10-{i + 1:02d}] факт — [[sources/{i}-x]]\n" for i in range(cnt))
            (tmp_path / kind / f"{wiki.slugify(name)}.md").write_text(f"# {name}\n\n{bullets}")


def test_tags_and_title_entities_match_wiki_pages(tmp_path, monkeypatch):
    _wiki(tmp_path, monkeypatch, entities=["Ollama", "Claude Code", ("n8n", 2)], concepts=[("self-hosting", 3), "rag"])
    slugs = [(p["kind"], p["slug"]) for p in related_pages(
        "Running Claude Code with Ollama locally", ["Self-Hosting", "LLMs", "ollama"], ttl=0, limit=5)]
    assert slugs == [("concepts", "self-hosting"), ("entities", "ollama"), ("entities", "claude-code")]
    assert related_pages("nothing here", ["rust"], ttl=0) == []
    # limit режет хвост, порядок — теги раньше заголовка
    assert [p["slug"] for p in related_pages("n8n and Ollama", ["self-hosting"], ttl=0, limit=2)] == [
        "self-hosting", "n8n",
    ]


def test_links_point_at_the_wiki_page(tmp_path, monkeypatch):
    _wiki(tmp_path, monkeypatch, entities=[("n8n", 2), "Кибербезопасность"])
    monkeypatch.setattr(wiki.settings, "public_base_url", "http://host:8000/")
    (page,) = related_pages("Automating with n8n", [], ttl=0)
    assert page["url"] == "http://host:8000/wiki?kind=entities&slug=n8n" and page["title"] == "n8n"
    assert page["n"] == 2
    (cyr,) = related_pages("x", ["кибербезопасность"], ttl=0)  # кириллица в URL — процентами
    assert cyr["url"].endswith("slug=%D0%BA%D0%B8%D0%B1%D0%B5%D1%80%D0%B1%D0%B5%D0%B7%D0%BE%D0%BF%D0%B0%D1%81%D0%BD%D0%BE%D1%81%D1%82%D1%8C")


def test_noise_does_not_match_pages(tmp_path, monkeypatch):
    """Мусорный тег и «...» не ведут на untitled; «C» из C++ и «meta» из meta-analysis — не сущности;
    одиночное слово заголовка требует страницы с ≥2 источниками."""
    _wiki(tmp_path, monkeypatch, entities=["untitled", "C", "Meta", ("Apple", 1), ("Ollama", 2)])
    assert related_pages("Meta-analysis of C++ trials ... today", ["###", "x" * 80], ttl=0) == []
    assert related_pages("Apple cider vinegar study", [], ttl=0) == []
    assert [p["slug"] for p in related_pages("Ollama 0.5 released", [], ttl=0)] == ["ollama"]
    assert [p["slug"] for p in related_pages("x", ["apple"], ttl=0)] == ["apple"]  # тег — явный сигнал


def test_heavier_pages_first_and_one_page_per_slug(tmp_path, monkeypatch):
    """rust есть и как сущность (35 источников), и как концепт (1) — одна строка, тяжёлая;
    порядок выдачи по весу, а не по порядку тегов; H1 страницы — как заголовок ссылки."""
    _wiki(tmp_path, monkeypatch, entities=[("Rust", 35), ("Go", 7)], concepts=[("rust", 1), ("Self-hosting", 12)])
    pages = related_pages("Go service", ["go", "self-hosting", "rust"], ttl=0, limit=5)
    assert [(p["kind"], p["slug"], p["n"]) for p in pages] == [
        ("entities", "rust", 35), ("concepts", "self-hosting", 12), ("entities", "go", 7),
    ]
    assert pages[0]["title"] == "Rust"


def test_empty_wiki_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki.settings, "wiki_dir", str(tmp_path / "none"))
    assert related_pages("Ollama", ["ollama"], ttl=0) == []
