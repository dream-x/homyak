"""Аналитика по накопленной ленте (RAG): вопрос → семантический поиск по новостям → выжимка LLM.

Мы храним всё: заголовок, текст, саммари, дату, эмбеддинг (Qdrant, bge-m3). Поэтому на любой
вопрос («что с нефтью в мире») можно собрать актуальную выжимку из того, что реально было в ленте.
Ответ строго заземлён на найденные новости — LLM не досочиняет.
"""

from __future__ import annotations

from sqlalchemy import bindparam, text

from homyak.adapters.analyzers.embedder import EmbedderAnalyzer
from homyak.core.config import settings
from homyak.core.llm import OllamaLLM
from homyak.core.textutils import strip_html
from homyak.storage.db import SessionFactory
from homyak.storage.qdrant import QdrantStore

ASK_SYSTEM = (
    "Ты — аналитик. По ПОДБОРКЕ новостей ниже ответь на вопрос пользователя на русском: "
    "дай сжатую, но содержательную выжимку текущего положения дел. Опирайся ТОЛЬКО на "
    "приведённые новости, ничего не добавляй от себя и не выдумывай факты.\n\n"
    "Структура ответа:\n"
    "• 2–4 предложения сути — что происходит по этому вопросу сейчас.\n"
    "• Ключевые события/факты с ДАТАМИ (самые свежие важнее; отмечай динамику).\n"
    "• Если данные в подборке противоречат друг другу или устарели — скажи об этом прямо.\n\n"
    "Если в подборке нет ответа на вопрос — честно напиши, что релевантных новостей в ленте мало."
)


async def answer_question(question: str, max_items: int = 28) -> dict:
    """Вопрос → выжимка по ленте. Возвращает {answer, n, sources}."""
    q = (question or "").strip()
    if not q:
        return {"answer": None, "n": 0, "sources": []}

    qdrant = QdrantStore(settings.qdrant_url)
    try:
        vec = await EmbedderAnalyzer(qdrant)._embed(q)
        # score_threshold низкий: 0.88 — это порог дедупа (почти дубли), а нам нужны просто
        # тематически близкие. limit с запасом — потом отсортируем по свежести и обрежем.
        hits = await qdrant.search_similar(vec, limit=max_items + 15, score_threshold=0.30)
    finally:
        await qdrant.close()
    if not hits:
        return {"answer": None, "n": 0, "sources": []}

    ids = [h[0] for h in hits]
    async with SessionFactory() as s:
        rows = (
            await s.execute(
                text(
                    "select id, title, summary, text, published_at, feed_name, url"
                    " from news_items where id in :ids and processed_at is not null"
                ).bindparams(bindparam("ids", value=ids, expanding=True))
            )
        ).all()
    if not rows:
        return {"answer": None, "n": 0, "sources": []}

    # Свежие сверху: LLM должна видеть «текущий момент» первым и взвешивать по датам.
    rows = sorted(rows, key=lambda r: (r.published_at is not None, r.published_at), reverse=True)
    rows = rows[:max_items]

    lines = []
    for r in rows:
        d = r.published_at.strftime("%Y-%m-%d") if r.published_at else "—"
        body = (r.summary or strip_html(r.text) or "")[:280].replace("\n", " ")
        src = "@" + r.feed_name[3:] if (r.feed_name or "").startswith("tw_") else (r.feed_name or "")
        lines.append(f"[{d}] {r.title} — {body} ({src})")
    context = f"Вопрос: {q}\n\nНовости (свежие сверху):\n" + "\n".join(lines)

    llm = OllamaLLM(model=settings.summary_model, fallback=settings.summary_fallback_model)
    answer = await llm.chat_text(ASK_SYSTEM, context, think=False)
    return {
        "answer": answer,
        "n": len(rows),
        "sources": [
            {
                "title": r.title,
                "url": r.url,
                "date": r.published_at.strftime("%Y-%m-%d") if r.published_at else None,
            }
            for r in rows[:8]
        ],
    }
