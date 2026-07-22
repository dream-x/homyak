"""Query по LLM-вике: вопрос → ответ из СИНТЕЗИРОВАННЫХ страниц (не по сырым статьям).

Вика мала и курируема (⭐/👍), поэтому на малом масштабе хватает index.md + пары релевантных
страниц в контексте (как у Karpathy). Релевантность — по совпадению слов запроса (дёшево, без
эмбеддингов). Пусто → None, и вызывающий (api /search/answer) падает на RAG по всей ленте.
"""

from __future__ import annotations

import re

from homyak.core.config import settings
from homyak.core.llm import OllamaLLM
from homyak.core.textutils import detect_lang
from homyak.core import wiki

_WORD = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ]{3,}")
MAX_PAGES = 8
MAX_CTX = 12000

_SYS_RU = (
    "Ты отвечаешь по ЛИЧНОЙ базе знаний (вики), собранной из сохранённого пользователем. "
    "Отвечай на русском, ТОЛЬКО по приведённым страницам вики, без домыслов. Сжато и по делу; "
    "ссылайся на страницы в скобках, напр. (concepts/rag). Если в вике нет ответа — так и скажи."
)
_SYS_EN = (
    "You answer from a PERSONAL knowledge base (wiki) built from what the user saved. "
    "Answer in English, ONLY from the wiki pages below, no speculation. Concise; cite pages in "
    "parentheses, e.g. (concepts/rag). If the wiki lacks the answer, say so plainly."
)


def _score(text: str, words: list[str]) -> int:
    low = text.lower()
    return sum(low.count(w) for w in words)


async def answer_from_wiki(question: str, *, max_pages: int = MAX_PAGES) -> dict | None:
    q = (question or "").strip()
    if not q:
        return None
    words = list({w.lower() for w in _WORD.findall(q)})
    if not words:
        return None

    # собираем страницы (кроме sources — они сырее; концепты/сущности синтезированы)
    candidates: list[tuple[int, str, str]] = []
    for kind in ("concepts", "entities", "sources"):
        for slug in wiki.list_pages(kind):
            body = wiki.read_page(kind, slug) or ""
            sc = _score(body, words) + (3 if any(w in slug.lower() for w in words) else 0)
            if sc > 0:
                candidates.append((sc, f"{kind}/{slug}", body))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    picked = candidates[:max_pages]

    ctx_parts = []
    total = 0
    for _sc, ref, body in picked:
        chunk = f"\n\n=== {ref} ===\n{body.strip()}"
        if total + len(chunk) > MAX_CTX:
            break
        ctx_parts.append(chunk)
        total += len(chunk)
    context = f"Вопрос: {q}\n\nСтраницы вики:{''.join(ctx_parts)}"

    system = _SYS_RU if detect_lang(q) == "ru" else _SYS_EN
    llm = OllamaLLM(model=settings.summary_model, fallback=settings.summary_fallback_model)
    answer = await llm.chat_text(system, context, think=False)
    return {
        "answer": (answer or "").strip() or None,
        "n": len(picked),
        "sources": [{"title": ref, "url": None, "date": None} for _sc, ref, _b in picked],
    }
