"""Telegraph (telegra.ph) — публикация текста статьи как страницы с Instant View в Telegram.

Ссылка на telegra.ph открывается нативной читалкой прямо в приложении (без ухода в браузер).
"""

from __future__ import annotations

import json

import httpx
import structlog

log = structlog.get_logger(__name__)

_API = "https://api.telegra.ph"


async def create_account() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{_API}/createAccount",
                data={"short_name": "Homyak", "author_name": "Homyak"},
            )
            d = r.json()
        return d["result"]["access_token"] if d.get("ok") else None
    except Exception as e:
        log.warning("telegraph_account_failed", error=str(e))
        return None


def _to_nodes(text: str) -> list[dict]:
    nodes: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            nodes.append({"tag": "p", "children": [line]})
    return nodes or [{"tag": "p", "children": [text[:200] or "—"]}]


async def create_page(
    token: str, title: str | None, text: str, author: str | None = None, url: str | None = None
) -> str | None:
    """Создаёт страницу telegra.ph, возвращает её URL. None при ошибке."""
    content = _to_nodes(text)
    if url:  # ссылка на оригинал первым абзацем
        content.insert(
            0, {"tag": "p", "children": [{"tag": "a", "attrs": {"href": url}, "children": ["🔗 Оригинал"]}]}
        )
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{_API}/createPage",
                data={
                    "access_token": token,
                    "title": (title or "Статья")[:256],
                    "author_name": (author or "Homyak")[:128],
                    "content": json.dumps(content, ensure_ascii=False),
                    "return_content": "false",
                },
            )
            d = r.json()
        return d["result"]["url"] if d.get("ok") else None
    except Exception as e:
        log.warning("telegraph_page_failed", error=str(e))
        return None
