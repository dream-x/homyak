from homyak.core.urls import normalize_url


def test_none_and_empty():
    assert normalize_url(None) is None
    assert normalize_url("   ") is None


def test_strips_tracking_and_fragment_sorts_query():
    got = normalize_url("https://www.Example.com/a/b/?utm_source=x&b=2&a=1#frag")
    assert got == "https://example.com/a/b?a=1&b=2"


def test_default_ports_and_www():
    assert normalize_url("https://example.com:443/x") == "https://example.com/x"
    assert normalize_url("http://example.com:80/x") == "http://example.com/x"
    assert normalize_url("http://www.example.com/") == "http://example.com/"


def test_idempotent():
    once = normalize_url("https://www.example.com/a/?gclid=zzz&q=1")
    twice = normalize_url(once)
    assert once == twice == "https://example.com/a?q=1"


def test_two_urls_same_after_normalization():
    a = normalize_url("https://example.com/post?utm_medium=rss&id=5")
    b = normalize_url("https://www.example.com/post/?id=5&fbclid=abc")
    assert a == b
