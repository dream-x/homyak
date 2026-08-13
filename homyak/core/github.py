"""README репозитория как текст статьи.

У GitHub-источников RSSHub отдаёт одну строку описания, а страница репозитория плохо
поддаётся общему экстрактору: у 30-45% записей в базе оседало меньше 400 символов, и
«описанием» служил сырой английский блёрб. Тянем README через API — он отдаёт файл при
любом имени и любой ветке, без угадывания master/main.

Markdown чистим: бейджи, картинки и HTML-обвязка занимают верх почти каждого README и
съедают контекст модели, не неся смысла.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
import structlog

from homyak.core.config import settings

log = structlog.get_logger(__name__)

_API = "https://api.github.com/repos/{owner}/{repo}/readme"
# Резервный путь на случай, когда API отвечает отказом (без токена — 60 запросов в час на IP,
# поток gh_search_* выбирает это быстро). Тут имя файла приходится перебирать: raw отдаёт
# конкретный путь, а не «какой бы README ни лежал», как API.
_RAW = "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}"
_RAW_NAMES = ("README.md", "readme.md", "Readme.md", "README.rst", "README.txt", "README")

# Служебные пути: это не репозитории, README у них нет.
_NOT_REPOS = frozenset(
    {
        "features", "topics", "trending", "collections", "events", "sponsors", "about",
        "pricing", "enterprise", "explore", "marketplace", "settings", "notifications",
        "orgs", "users", "search", "login", "join", "apps", "blog",
    }
)

_BADGE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")  # [![badge](img)](link)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_ANCHOR = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # [текст](ссылка) → текст
_BLANKS = re.compile(r"\n{3,}")


def repo_of(url: str | None) -> tuple[str, str] | None:
    """github.com/owner/repo (в любом виде: /tree/main, /blob/…, #anchor) → (owner, repo)."""
    if not url:
        return None
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() in _NOT_REPOS:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def clean_markdown(md: str) -> str:
    """Markdown → читаемый текст: без бейджей, картинок и HTML, ссылки — своим текстом."""
    text = _HTML_COMMENT.sub(" ", md)
    text = _BADGE.sub(" ", text)
    text = _IMAGE.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    text = _ANCHOR.sub(r"\1", text)
    text = re.sub(r"^[#>\s]*", "", text, flags=re.MULTILINE)  # заголовки и цитаты
    text = re.sub(r"[ \t]+", " ", text)
    return _BLANKS.sub("\n\n", text).strip()


async def fetch_readme(url: str, timeout: float = 20.0) -> str | None:
    """README репозитория текстом. None — если ссылка не на репозиторий или он недоступен."""
    found = repo_of(url)
    if not found:
        return None
    owner, repo = found
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "homyak"}
    # Токен не обязателен, но без него GitHub даёт 60 запросов в час на IP — на потоке
    # gh_search_* это упирается в лимит за считанные минуты.
    if settings.github_access_token:
        headers["Authorization"] = f"Bearer {settings.github_access_token}"

    targets = [(_API.format(owner=owner, repo=repo), headers)]
    targets += [
        (_RAW.format(owner=owner, repo=repo, name=n), {"User-Agent": "homyak"}) for n in _RAW_NAMES
    ]
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for target, hdrs in targets:
            try:
                resp = await client.get(target, headers=hdrs)
                resp.raise_for_status()
                text = clean_markdown(resp.text)
                if text:
                    log.info("github_readme_fetched", repo=f"{owner}/{repo}", chars=len(text))
                    return text
            except Exception as e:
                log.debug(
                    "github_readme_failed", url=target, error=f"{type(e).__name__}: {e}"[:120]
                )
    return None
