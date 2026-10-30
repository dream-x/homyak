"""Отдельный компонент: скачивание и извлечение полного текста статьи по URL.

RSS часто отдаёт только огрызок (а HN — вообще ссылку «Comments»). Тянем страницу и вытаскиваем
основной текст через trafilatura (топовый экстрактор). Если сайт блокирует бота (403) или рендерит
контент через JS — фолбэк на reader-сервис (r.jina.ai), который тянет и чистит страницу серверно.
Best-effort — при любой проблеме None.
"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from urllib.parse import urlparse

import feedparser
import httpx
import structlog

from homyak.core.config import settings
from homyak.core.github import fetch_readme
from homyak.core.titles import title_from_url

try:
    import trafilatura
except Exception:  # pragma: no cover
    trafilatura = None

log = structlog.get_logger(__name__)

# Реальный браузерный UA — кастомный homyak/0.1 многие сайты режут как бота (403).
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_MIN_CHARS = 400  # ниже — считаем извлечение неудачным, пробуем reader-фолбэк
_SKIP_HOSTS = tuple(h.strip().lower() for h in settings.article_skip_hosts.split(",") if h.strip())


def _skipped(url: str) -> bool:
    # Матч по границе хоста, НЕ по подстроке: иначе 'x.com' поймает 'phoronix.com'/'netflix.com'.
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS)


# Reddit HTML нам больше не отдаёт ни в каком виде (проверено с VM 31.10.2026): www шлёт
# JS-заглушку на 8 КБ, old.reddit требует логин (reason=lor2), через прокси — «Blocked»,
# reader r.jina.ai отвечает 451. Раньше старый интерфейс брался «по длине ответа», и страница
# логина на 190 КБ побеждала заглушку — из такой обвязки и родился заголовок «Skip to main
# content» у двух ручных ссылок. Что отдаётся — Atom-лента поста /r/<sub>/comments/<id>/.rss
# (с браузерным UA; с честным homyak/1.0 — 429). Первая запись — сам пост: заголовок, автор,
# selftext между маркерами SC_OFF/SC_ON; дальше идут комментарии.
_REDDIT_RE = re.compile(r"^https?://(?:(?:www|old|new|np)\.)?reddit\.com/", re.IGNORECASE)
_REDDIT_POST_RE = re.compile(
    r"^https?://(?:(?:www|old|new|np)\.)?reddit\.com/r/([^/?#]+)/comments/([a-z0-9]+)",
    re.IGNORECASE,
)
_SC_BLOCK = re.compile(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", re.DOTALL)
_PREVIEW_TABLE = re.compile(r"<table>.*?</table>", re.DOTALL | re.IGNORECASE)
_BLOCK_TAG = re.compile(r"</?(?:p|br|li|tr|h[1-6]|div|blockquote|pre)\b[^>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")
_SUBMITTED = re.compile(r"^\s*submitted by\b.*$|^\s*\[link\]\s*\[comments\]\s*$", re.MULTILINE)


def _html_to_text(raw: str) -> str:
    """HTML selftext → текст с абзацами. textutils.strip_html схлопывает и переводы строк,
    а Разбору и саммари абзацы нужны."""
    text = _BLOCK_TAG.sub("\n", raw)
    text = unescape(_ANY_TAG.sub(" ", text))
    text = _SUBMITTED.sub("", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_reddit_atom(xml: str) -> tuple[str | None, str | None]:
    """(selftext, заголовок) из Atom-ленты поста. Пост-ссылка без текста → (None, заголовок)."""
    parsed = feedparser.parse(xml)
    if not parsed.entries:
        return None, None
    post = parsed.entries[0]
    title = (post.get("title") or "").strip() or None
    content = post.get("content") or []
    raw = (content[0].get("value") if content else None) or post.get("summary") or ""
    m = _SC_BLOCK.search(raw)
    body = m.group(1) if m else _PREVIEW_TABLE.sub("", raw)
    text = _html_to_text(body)
    return (text or None), title


async def _fetch_reddit(url: str, timeout: float) -> tuple[str | None, str | None]:
    m = _REDDIT_POST_RE.match(url)
    if not m:  # ссылка-шаринг /r/X/s/ABC: www отдаёт заглушку, но редирект на /comments/ честный
        _, final = await _fetch_html(url, timeout)
        m = _REDDIT_POST_RE.match(final)
    if not m:
        return None, None
    sub, pid = m.group(1), m.group(2)
    xml, _ = await _fetch_html(f"https://www.reddit.com/r/{sub}/comments/{pid}/.rss", timeout)
    text, title = await asyncio.to_thread(parse_reddit_atom, xml) if xml else (None, None)
    if text or title:
        log.info("reddit_via_atom", url=url[:100], chars=len(text or ""), title=(title or "")[:60])
        return text, title
    # Лента не отдалась (429/блок). Заголовок есть хотя бы в слаге развёрнутого адреса —
    # лучше «The ultimate 2026 opensource ai stack» из URL, чем обвязка страницы-заглушки.
    slug = title_from_url(m.string)
    return None, (slug if slug and slug.lower() != pid.lower() else None)


# x.com отвечает нам таймаутом и живёт в ARTICLE_SKIP_HOSTS: твиты и так приходили текстом
# из RSSHub, тянуть их было незачем. У ссылки, принесённой руками, этого текста нет — берём
# через публичное зеркало API (авторизации не требует; vxtwitter с VM недоступен).
_TWEET_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([^/]+)/status/(\d+)", re.IGNORECASE
)
_FX = "https://api.fxtwitter.com/{user}/status/{sid}"


async def _fetch_tweet(url: str, timeout: float) -> str | None:
    m = _TWEET_RE.match(url or "")
    if not m:
        return None
    user, sid = m.group(1), m.group(2)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=_HEADERS) as c:
            resp = await c.get(_FX.format(user=user, sid=sid))
            resp.raise_for_status()
            tweet = (resp.json() or {}).get("tweet") or {}
    except Exception as e:
        log.debug("tweet_fetch_failed", url=url, error=f"{type(e).__name__}: {e}"[:120])
        return None

    parts = [tweet.get("text") or ""]
    quoted = tweet.get("quote") or {}
    if quoted.get("text"):  # цитируемый твит — половина смысла обычно в нём
        author = ((quoted.get("author") or {}).get("screen_name")) or "?"
        parts.append(f"\n\nЦитирует @{author}:\n{quoted['text']}")
    text = "".join(parts).strip()
    if text:
        log.info("tweet_fetched", url=url[:100], chars=len(text))
    return text or None


_META_PATTERNS = (
    r'<meta[^>]+(?:property|name)=["\']{key}["\'][^>]+content=["\']([^"\']*)["\']',
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{key}["\']',
)


def _meta(html: str, key: str) -> str | None:
    """og:/twitter: разметка — последний рубеж, когда экстрактор не взял ничего.

    Атрибуты в разметке идут в любом порядке, поэтому шаблона два. Именно эти теги читает
    Telegram, когда рисует превью ссылки, — то есть данные есть даже там, где текста нет.
    """
    for pattern in _META_PATTERNS:
        m = re.search(pattern.format(key=re.escape(key)), html, re.IGNORECASE)
        if m and m.group(1).strip():
            return unescape(m.group(1)).strip()
    return None


def _strip_reader(md: str | None) -> str | None:
    """r.jina.ai отдаёт 'Title: …\\nURL Source: …\\nMarkdown Content:\\n<текст>' — берём тело."""
    if not md:
        return None
    marker = "Markdown Content:"
    idx = md.find(marker)
    body = md[idx + len(marker) :] if idx != -1 else md
    return body.strip() or None


async def _fetch_html(url: str, timeout: float) -> tuple[str | None, str]:
    """(html, конечный url после редиректов). Конечный нужен, чтобы знать НАСТОЯЩИЙ адрес:
    короткие ссылки-шаринги вида reddit.com/r/X/s/ABC разворачиваются редиректом, и Atom-ленту
    поста надо просить уже по развёрнутому /comments/-адресу."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=_HEADERS
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text, str(resp.url)
    except httpx.HTTPStatusError as e:
        # Редиректы уже пройдены: конечный адрес известен и при 403/429, и он нужен —
        # reddit-шаринг разворачивается в /comments/, даже когда сама страница закрыта.
        log.debug("article_fetch_failed", url=url, error=str(e))
        return None, str(e.response.url)
    except Exception as e:
        log.debug("article_fetch_failed", url=url, error=str(e))
        return None, url


async def _reader_fallback(url: str, timeout: float) -> str | None:
    """Reader-сервис: тянет и чистит страницу серверно (обходит бот-блок/JS)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get("https://r.jina.ai/" + url, headers={"User-Agent": _UA})
            resp.raise_for_status()
            return _strip_reader(resp.text)
    except Exception as e:
        log.debug("article_reader_failed", url=url, error=str(e))
        return None


async def fetch_page(
    url: str, timeout: float = 20.0, allow_social: bool = False
) -> tuple[str | None, str | None]:
    """URL → (текст, заголовок). Заголовок — только из og:/twitter:, если он там есть.

    Заголовок возвращаем отдельно, потому что он ценнее сгенерированного: og:title — это то,
    как страницу назвал автор, и ровно его показывает Telegram в превью ссылки.

    allow_social включает поход за твитом через зеркало API. По умолчанию выключен: твиты
    приходят текстом из RSSHub, и дёргать чужой публичный сервис на каждый из сотен
    твиттер-источников незачем. Включаем там, где текста нет вовсе.
    """
    if allow_social:
        tweet = await _fetch_tweet(url, timeout)
        if tweet:
            return tweet, None

    if not url or _skipped(url):  # известная бот-стена — не тратим попытки
        return None, None

    # Репозиторий — особый случай: у страницы GitHub общий экстрактор берёт обвязку вместо
    # содержания, поэтому README запрашиваем напрямую. Здесь, а не в ⭐-канале: полный текст
    # нужен всем — теггеру, саммари, судье, поиску и вике.
    readme = await fetch_readme(url, timeout)
    if readme and len(readme) >= _MIN_CHARS:
        return readme, None

    # Reddit — только через Atom-ленту поста: общий путь даёт заглушку, логин или «Blocked»,
    # а reader — 451; ничего из этого текстом считать нельзя, и фолбэки ниже не для него.
    if _REDDIT_RE.match(url):
        return await _fetch_reddit(url, timeout)

    text, title = "", None
    html, _ = await _fetch_html(url, timeout)
    if html:
        title = _meta(html, "og:title") or _meta(html, "twitter:title")
        if trafilatura is not None:
            try:
                extracted = await asyncio.to_thread(
                    trafilatura.extract,
                    html,
                    include_comments=False,
                    include_tables=False,
                    favor_recall=True,
                )
                text = (extracted or "").strip()
            except Exception as e:
                log.debug("article_extract_failed", url=url, error=str(e))

    # бот-блок (403) / JS-рендер → пробуем reader, берём если он дал больше
    if len(text) < _MIN_CHARS and settings.article_reader_fallback:
        reader = await _reader_fallback(url, timeout + 10)
        if reader and len(reader) > len(text):
            text = reader

    # Ни экстрактор, ни reader ничего не дали — собираем хоть что-то из og-разметки.
    # Куцая карточка с настоящим заголовком лучше, чем «(без заголовка)» и пустота.
    if len(text) < _MIN_CHARS and html:
        desc = _meta(html, "og:description") or _meta(html, "twitter:description")
        fallback = "\n\n".join(x for x in (title, desc) if x)
        if len(fallback) > len(text):
            text = fallback
            log.info("article_from_meta", url=url[:120], chars=len(text))

    return (text or None), title


async def fetch_article(url: str, timeout: float = 20.0) -> str | None:
    """Только текст — для мест, где заголовок не нужен (⭐-канал, бэкфиллы)."""
    text, _ = await fetch_page(url, timeout)
    return text
