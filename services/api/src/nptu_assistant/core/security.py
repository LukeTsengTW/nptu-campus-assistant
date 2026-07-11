from __future__ import annotations

import hmac
from urllib.parse import urlparse


def is_allowed_nptu_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and (host == "nptu.edu.tw" or host.endswith(".nptu.edu.tw"))


def secrets_match(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
