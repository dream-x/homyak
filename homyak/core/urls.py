"""Нормализация URL для дедупликации. Чистая функция — легко тестируется."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "cmpid",
    "spm",
}


def normalize_url(url: str | None) -> str | None:
    """Каноничный вид URL: lower scheme/host, без www, tracking-параметров, fragment'а,
    хвостового слэша; query-параметры отсортированы. Идемпотентна."""
    if not url or not url.strip():
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return url.strip()  # относительный/битый — не трогаем

    scheme = (parts.scheme or "http").lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    q = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS
    ]
    q.sort()
    query = urlencode(q)

    return urlunsplit((scheme, netloc, path, query, ""))
