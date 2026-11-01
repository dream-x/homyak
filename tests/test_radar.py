from datetime import date

import httpx
import pytest

from homyak.core.radar import RateLimited, fetch_repo, is_rate_limit, repo_key, velocity


def test_repo_key_normalises_github_urls():
    assert repo_key("https://github.com/Anthropics/Claude-Code/tree/main/src") == "anthropics/claude-code"
    assert repo_key("https://www.github.com/a/b.git") == "a/b"
    assert repo_key("https://github.com/trending/python") is None
    assert repo_key("https://gitlab.com/a/b") is None
    assert repo_key(None) is None


def test_velocity_uses_first_and_last_snapshot():
    v = velocity([(date(2026, 10, 25), 100), (date(2026, 10, 31), 400), (date(2026, 10, 28), 200)])
    assert v == {"stars": 400, "gained": 300, "per_day": 50.0, "span_days": 6}
    assert velocity([(date(2026, 10, 25), 100)]) is None
    assert velocity([]) is None
    same = velocity([(date(2026, 10, 25), 5), (date(2026, 10, 25), 9)])  # тот же день → span 1
    assert same["span_days"] == 1 and same["per_day"] == 4.0


def test_rate_limit_is_read_from_headers_not_from_a_bare_403():
    assert is_rate_limit(429, {})
    assert is_rate_limit(403, {"x-ratelimit-remaining": "0"})
    assert is_rate_limit(403, {"retry-after": "60"})
    assert not is_rate_limit(403, {"x-ratelimit-remaining": "41"})  # TOS-блок, SAML — не лимит
    assert not is_rate_limit(404, {})


async def _client(status: int, headers: dict | None = None, body: dict | None = None):
    def handler(request):
        return httpx.Response(status, headers=headers or {}, json=body or {"message": "x"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_repo_blocked_is_missing_and_limited_raises():
    async with await _client(403, {"x-ratelimit-remaining": "41"}, {"message": "Repository access blocked"}) as c:
        assert await fetch_repo("a/b", c) is None
    async with await _client(403, {"x-ratelimit-remaining": "0"}) as c:
        with pytest.raises(RateLimited):
            await fetch_repo("a/b", c)
    async with await _client(200, body={"stargazers_count": 7, "forks_count": 1, "pushed_at": "2026-10-30T10:00:00Z"}) as c:
        got = await fetch_repo("a/b", c)
        assert (got["stars"], got["forks"], got["open_issues"]) == (7, 1, 0)
        assert got["pushed_at"].isoformat() == "2026-10-30T10:00:00+00:00"
