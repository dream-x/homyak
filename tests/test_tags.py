from homyak.core.tags import ALIASES, canonical_tags, normalize_tag, variants


def test_form_is_normalised():
    assert normalize_tag("  AI Agents ") == "ai-agents"
    assert normalize_tag("ai_agents") == "ai-agents"
    assert normalize_tag("#AI-agents") == "ai-agents"
    assert normalize_tag("ai--agents-") == "ai-agents"
    assert normalize_tag("c++") == "c++"
    assert normalize_tag("m&a") == "mergers"


def test_synonyms_collapse_to_one_tag():
    assert normalize_tag("LLMs") == "llm"
    assert normalize_tag("large language models") == "llm"
    assert normalize_tag("Federal Reserve") == "fed"
    assert normalize_tag("tariff") == "tariffs"
    assert normalize_tag("self hosted") == "selfhosted"


def test_junk_is_dropped():
    assert normalize_tag(None) is None
    assert normalize_tag("   ") is None
    assert normalize_tag("#") is None
    assert normalize_tag("x" * 41) is None


def test_alias_targets_are_themselves_canonical():
    for alias, canon in ALIASES.items():
        assert normalize_tag(canon) == canon, (alias, canon)
        assert alias != canon


def test_canonical_list_dedupes_and_keeps_order():
    assert canonical_tags(["LLMs", "rust", "llm", "", "Rust-lang", "ai agents"]) == [
        "llm",
        "rust",
        "ai-agents",
    ]
    assert canonical_tags(None) == []


def test_variants_cover_historical_spellings():
    v = variants("LLM")
    assert v[0] == "llm" and {"llms", "large-language-models"} <= set(v)
    assert variants("rust") == ["rust", "rust-lang", "rustlang"]
    assert variants("prompt-injection") == ["prompt-injection"]
