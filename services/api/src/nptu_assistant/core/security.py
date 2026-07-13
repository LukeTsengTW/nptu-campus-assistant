from __future__ import annotations

import hmac
from collections.abc import Collection
from urllib.parse import urlsplit, urlunsplit


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
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def secrets_match(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
