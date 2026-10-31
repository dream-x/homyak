"""Канонизация тегов. Чистые функции, без БД.

LLM-теггер пишет одну тему десятком способов: llm / llms / large-language-models, ai agents /
ai-agents / agentic-ai. В проде 11,7 тыс. уникальных тегов, 6,8 тыс. из них встречаются один раз,
и любой подсчёт «по темам» (тренды, таймлайны, пульс) без свода даёт шум. Здесь — форма
(регистр, пробелы, подчёркивания, решётки) и короткий словарь синонимов для самых частых тем.
Теггер применяет это при записи; читатели (тренды) — при чтении, потому что история тегов
не переписывается.
"""

from __future__ import annotations

import re

_SEP = re.compile(r"[\s_/]+")
_BAD = re.compile(r"[^0-9a-zA-Zа-яёА-ЯЁ+#.&-]+")
MAX_TAG_LEN = 40

# синоним → каноническая форма; значения тоже канонические (цепочек нет)
ALIASES: dict[str, str] = {
    # AI / LLM
    "llms": "llm",
    "large-language-model": "llm",
    "large-language-models": "llm",
    "language-models": "llm",
    "artificial-intelligence": "ai",
    "genai": "ai",
    "generative-ai": "ai",
    "ai-agent": "ai-agents",
    "agents": "ai-agents",
    "agentic-ai": "ai-agents",
    "agentic": "ai-agents",
    "multi-agent": "ai-agents",
    "machine-learning": "ml",
    "machinelearning": "ml",
    "deep-learning": "ml",
    "retrieval-augmented-generation": "rag",
    "model-context-protocol": "mcp",
    # engineering
    "cybersecurity": "security",
    "cyber-security": "security",
    "infosec": "security",
    "appsec": "security",
    "self-hosted": "selfhosted",
    "self-hosting": "selfhosted",
    "selfhosting": "selfhosted",
    "open-source": "opensource",
    "open-source-software": "opensource",
    "oss": "opensource",
    "k8s": "kubernetes",
    "database": "databases",
    "db": "databases",
    "dev-tools": "devtools",
    "developer-tools": "devtools",
    "tooling": "devtools",
    "observability-tools": "observability",
    "monitoring": "observability",
    "rust-lang": "rust",
    "rustlang": "rust",
    "golang": "go",
    "go-lang": "go",
    "distributed-system": "distributed-systems",
    "cloud-computing": "cloud",
    # business / markets
    "startup": "startups",
    "venture-capital": "venture",
    "vc": "venture",
    "m&a": "mergers",
    "mergers-and-acquisitions": "mergers",
    "acquisitions": "mergers",
    "earnings-report": "earnings",
    "earnings-call": "earnings",
    "earnings-reports": "earnings",
    "quarterly-results": "earnings",
    "federal-reserve": "fed",
    "the-fed": "fed",
    "interest-rate": "rates",
    "interest-rates": "rates",
    "rate-cut": "rates",
    "rate-cuts": "rates",
    "rate-hike": "rates",
    "tariff": "tariffs",
    "trade-tariffs": "tariffs",
    "trade-war": "tariffs",
    "cryptocurrency": "crypto",
    "cryptocurrencies": "crypto",
    "stock": "stocks",
    "stock-market": "stocks",
    "equities": "stocks",
    "oil-prices": "oil",
    "crude-oil": "oil",
    "crude": "oil",
    "gold-price": "gold",
    "geopolitical": "geopolitics",
    "macro-economics": "macro",
    "macroeconomics": "macro",
    "inflation-data": "inflation",
    "cpi": "inflation",
    "jobs-report": "labor-market",
    "employment": "labor-market",
    "unemployment": "labor-market",
    "real-estate": "realestate",
    "housing-market": "realestate",
    "housing": "realestate",
    # medical
    "healthcare": "health",
    "medicine": "medical",
    "neonatal": "neonatology",
    "preterm-birth": "prematurity",
    "premature-birth": "prematurity",
    "preterm": "prematurity",
}


def normalize_tag(raw: str | None) -> str | None:
    """«AI Agents» / «ai_agents» / «#ai-agents» → «ai-agents»; llms → llm. Пусто/мусор → None."""
    if not raw:
        return None
    tag = raw.strip().lower().lstrip("#").strip()
    tag = _SEP.sub("-", tag)
    tag = _BAD.sub("", tag)
    tag = re.sub(r"-{2,}", "-", tag).strip("-.")
    if not tag or len(tag) > MAX_TAG_LEN:
        return None
    return ALIASES.get(tag, tag)


def canonical_tags(tags: list[str] | None) -> list[str]:
    """Нормализует список, убирает дубли, порядок сохраняет (первый тег — главный, его мьютят)."""
    out: list[str] = []
    for raw in tags or []:
        tag = normalize_tag(raw)
        if tag and tag not in out:
            out.append(tag)
    return out


def variants(tag: str) -> list[str]:
    """Канон + его словарные синонимы. Произвольные формы (ai_agents, «AI Agents») сюда не входят —
    для поиска по истории нужен SQL-двойник (`canon_lateral`), а не список."""
    canon = normalize_tag(tag) or tag
    return [canon] + sorted(k for k, v in ALIASES.items() if v == canon and k != canon)


# --- SQL-двойник normalize_tag ---------------------------------------------------------------
# История тегов не переписывается, поэтому всё, что читает её по теме (тренды, подборка, affinity,
# мьюты), обязано сводить к канону В ЗАПРОСЕ: свод после агрегации считал одну историю дважды
# (llm + llms), а список синонимов не покрывает формы вроде ai_agents. Та же форма, тот же
# словарь (ALIASES уходит параметрами в unnest), равенство с Python проверяется DB-тестом.

ALIAS_CTE = (
    "tag_alias as (select * from unnest(cast(:tag_alias_raw as text[]),"
    " cast(:tag_alias_canon as text[])) as x(raw, canon))"
)


def alias_params() -> dict[str, list[str]]:
    """Параметры для ALIAS_CTE."""
    return {"tag_alias_raw": list(ALIASES), "tag_alias_canon": list(ALIASES.values())}


def form_sql(col: str) -> str:
    """Форма тега в SQL (регистр, #, разделители, мусор, длина) — без словаря. NULL для пустого."""
    f = (
        "trim(both '-.' from regexp_replace(regexp_replace(regexp_replace("
        f"btrim(ltrim(lower(btrim({col})), '#')), '[\\s_/]+', '-', 'g'),"
        " '[^0-9a-zа-яё+#.&-]+', '', 'g'), '-{2,}', '-', 'g'))"
    )
    return f"nullif(case when length({f}) <= {MAX_TAG_LEN} then {f} end, '')"


def canon_lateral(col: str, name: str = "c") -> str:
    """`cross join lateral (…) {name}` → `{name}.canon`: канон тега {col} (NULL для мусора).
    Требует ALIAS_CTE в том же запросе и alias_params() в параметрах."""
    return (
        f"cross join lateral (select coalesce(al.canon, f.norm) canon"
        f" from (select {form_sql(col)} norm) f"
        f" left join tag_alias al on al.raw = f.norm) {name}"
    )


async def spelling_map(session, where_sql: str, params: dict) -> tuple[list[str], list[str]]:
    """(raws, canons) для всех различных тегов записей, удовлетворяющих `where_sql`.

    Для больших окон (тренды, таймлайн) канон считается здесь, в Python, по ~10 тыс. различных
    написаний, а не SQL-регэкспом по миллиону строк: `canon_lateral` внутри `exists` в WHERE
    планировщик гонял на каждую строку окна (63 с на таймлайн за 90 дней). Результат уходит в
    запрос параметрами — `unnest(:raws, :canons)` для свода или `tags && :raws` для поиска
    написаний темы (GIN по tags). Паритет с SQL-двойником гарантирован: источник — normalize_tag.
    """
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(f"select distinct t from news_items, unnest(tags) t where {where_sql}"), params
        )
    ).all()
    raws, canons = [], []
    for (raw,) in rows:
        canon = normalize_tag(raw)
        if canon:
            raws.append(raw)
            canons.append(canon)
    return raws, canons


def spellings_of(raws: list[str], canons: list[str], wanted: list[str]) -> list[str]:
    """Все написания из карты, чей канон — одна из нужных тем."""
    want = set(wanted)
    return [r for r, c in zip(raws, canons) if c in want]
