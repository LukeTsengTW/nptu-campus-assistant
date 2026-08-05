from __future__ import annotations

import hmac
import re
from collections.abc import Collection
from urllib.parse import urlsplit, urlunsplit


_NPTU_PATH_COMMA_ALIAS = re.compile(r"%2c", re.IGNORECASE)
_NPTU_PAGE_CONTENT_PATH = re.compile(
    r"^/p/(?:404|406)-(?P<unit_id>\d+)-(?P<content_id>\d+)"
    r"(?:,[^/]+)?\.php$",
    re.IGNORECASE,
)


def is_allowed_nptu_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and (host == "nptu.edu.tw" or host.endswith(".nptu.edu.tw"))
    )


def is_allowed_source_url(url: str, allowed_hosts: Collection[str]) -> bool:
    if not is_allowed_nptu_url(url):
        return False
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(
        host == allowed_host or host.endswith(f".{allowed_host}")
        for value in allowed_hosts
        if (allowed_host := value.strip().lower().rstrip("."))
    )


def canonicalize_nptu_url(url: str) -> str:
    if not is_allowed_nptu_url(url):
        raise ValueError("URL 必須是安全的 NPTU 官方 HTTPS 網址")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = _NPTU_PATH_COMMA_ALIAS.sub(",", parsed.path or "/")
    return urlunsplit(("https", host, path, parsed.query, ""))


def nptu_content_identity(url: str) -> str:
    """Return a stable identity for equivalent NPTU Page content routes."""
    canonical_url = canonicalize_nptu_url(url)
    parsed = urlsplit(canonical_url)
    match = _NPTU_PAGE_CONTENT_PATH.fullmatch(parsed.path)
    if match is None:
        return canonical_url
    identity_path = (
        f"/p/content-{match.group('unit_id')}-{match.group('content_id')}.php"
    )
    return urlunsplit(("https", parsed.netloc, identity_path, parsed.query, ""))


def secrets_match(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
