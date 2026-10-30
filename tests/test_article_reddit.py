"""Reddit тянется только через Atom-ленту поста (tests/fixtures/reddit_post.atom — живой
ответ на /r/SATNA_PROJECT/comments/1xaz7tl/.rss, снятый с VM 31.10.2026)."""

from pathlib import Path

from homyak.core import article
from homyak.core.article import _REDDIT_POST_RE, _REDDIT_RE, parse_reddit_atom

_ATOM = (Path(__file__).parent / "fixtures" / "reddit_post.atom").read_text(encoding="utf-8")
_SHARE = "https://www.reddit.com/r/SATNA_PROJECT/s/5UFiVy8rXs"
_POST = "https://www.reddit.com/r/SATNA_PROJECT/comments/1xaz7tl/the_ultimate_2026_opensource_ai_stack_how_to/"
_ATOM_URL = "https://www.reddit.com/r/SATNA_PROJECT/comments/1xaz7tl/.rss"


def test_post_urls_are_recognised_across_hosts_and_share_links_are_not():
    m = _REDDIT_POST_RE.match(_POST + "?share_id=1&utm_source=share")
    assert m and (m.group(1), m.group(2)) == ("SATNA_PROJECT", "1xaz7tl")
    assert _REDDIT_POST_RE.match("https://old.reddit.com/r/rust/comments/1abc")
    assert _REDDIT_POST_RE.match("https://reddit.com/r/rust/comments/1abc/slug/")
    assert _REDDIT_POST_RE.match(_SHARE) is None
    assert _REDDIT_RE.match(_SHARE)
    assert _REDDIT_RE.match("https://notreddit.com/r/x/comments/1") is None


def test_atom_gives_the_post_text_and_title_but_not_the_comments():
    text, title = parse_reddit_atom(_ATOM)
    assert title and title.startswith("The Ultimate 2026 Open-Source AI Stack")
    assert text and len(text) > 400
    assert "Not bad!" not in text  # первый комментарий (/u/circuit_nayhem) в текст не попал
    assert "<" not in text and "[link]" not in text and "submitted by" not in text
    assert "\n" in text  # абзацы сохранены


def test_empty_feed_gives_nothing():
    assert parse_reddit_atom(
        "<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'></feed>"
    ) == (
        None,
        None,
    )


async def test_share_link_is_resolved_then_fetched_as_atom(monkeypatch):
    calls: list[str] = []

    async def fake_fetch_html(url, timeout):
        calls.append(url)
        if "/s/" in url:  # www отдаёт заглушку, но редирект на /comments/ честный
            return "<html><title>Reddit</title></html>", _POST + "?share_id=x&utm_medium=ios_app"
        assert url == _ATOM_URL
        return _ATOM, url

    monkeypatch.setattr(article, "_fetch_html", fake_fetch_html)

    text, title = await article.fetch_page(_SHARE)

    assert title and title.startswith("The Ultimate 2026")
    assert text and len(text) > 400
    assert calls == [_SHARE, _ATOM_URL]


async def test_dead_atom_falls_back_to_the_slug_title(monkeypatch):
    async def dead(url, timeout):
        return None, url

    monkeypatch.setattr(article, "_fetch_html", dead)

    assert await article.fetch_page(_POST) == (None, "The ultimate 2026 opensource ai stack how to")
    # ссылка-шаринг без редиректа развернуть нельзя — и выдумывать нечего
    assert await article.fetch_page(_SHARE) == (None, None)
    # адрес без слага: id постом не назовёшь
    assert await article.fetch_page("https://www.reddit.com/r/rust/comments/1abc") == (None, None)


async def test_blocked_post_page_still_resolves_the_share_link(monkeypatch):
    """share → 302 → страница поста отвечает 403 → Atom всё равно запрашивается по развёрнутому
    адресу: редирект пройден, и конечный URL известен даже при ошибке (находка Астры #004)."""
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/s/" in request.url.path:
            return httpx.Response(302, headers={"location": _POST + "?share_id=x"})
        if request.url.path.endswith("/.rss"):
            return httpx.Response(200, text=_ATOM)
        return httpx.Response(403, text="<html><title>Blocked</title></html>")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))

    text, title = await article.fetch_page(_SHARE)

    assert title and title.startswith("The Ultimate 2026")
    assert text and len(text) > 400
    assert [u.split("?")[0] for u in seen] == [_SHARE, _POST, _ATOM_URL]
