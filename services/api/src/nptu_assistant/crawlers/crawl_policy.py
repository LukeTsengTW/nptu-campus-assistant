from __future__ import annotations

from collections.abc import Collection
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# Resource suffixes are intentionally kept in one place.  The site-map
# repository and the live crawler must agree on whether a URL can be parsed as
# an HTML page; this is independent from whether an existing document is
# indexable by the document-retrieval pipeline.
NON_HTML_RESOURCE_SUFFIXES = frozenset(
    {
        ".7z",
        ".avi",
        ".css",
        ".csv",
        ".doc",
        ".docx",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".mov",
        ".mp3",
        ".mp4",
        ".odt",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".rar",
        ".svg",
        ".tar",
        ".tif",
        ".tiff",
        ".txt",
        ".webp",
        ".xls",
        ".xlsx",
        ".xml",
        ".zip",
    }
)

DOCUMENT_RESOURCE_SUFFIXES = frozenset(
    {".pdf", ".doc", ".docx", ".odt", ".xls", ".xlsx"}
)

FRONTIER_TRAP_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "jsessionid",
        "phpsessid",
        "redirect",
        "return",
        "session",
        "sessionid",
        "sid",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)


def is_document_resource_url(url: str) -> bool:
    """Return whether ``url`` identifies a document worth indexing as metadata."""

    return urlsplit(url).path.casefold().endswith(tuple(DOCUMENT_RESOURCE_SUFFIXES))


def is_frontier_trap_url(
    url: str,
    *,
    max_query_params: int = 6,
    max_query_length: int = 512,
    max_path_segments: int = 32,
    max_repeated_path_segment: int = 3,
) -> bool:
    """Bound URL shapes that commonly create unproductive crawl frontiers.

    The filter is deliberately conservative: ordinary query URLs and
    paginated official pages remain valid, while tracking/session parameters,
    repeated query keys, and path/query sizes that can create unbounded traps
    are rejected.
    """

    parsed = urlsplit(url)
    if len(parsed.query) > max_query_length:
        return True

    query_parts = [part for part in parsed.query.split("&") if part]
    if len(query_parts) > max_query_params:
        return True
    query_keys: list[str] = []
    for part in query_parts:
        key = part.split("=", 1)[0].casefold()
        query_keys.append(key)
        if key in FRONTIER_TRAP_QUERY_KEYS:
            return True
    if len(query_keys) != len(set(query_keys)):
        return True

    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) > max_path_segments:
        return True
    repeated = 1
    for previous, current in zip(segments, segments[1:]):
        if previous.casefold() == current.casefold():
            repeated += 1
            if repeated > max_repeated_path_segment:
                return True
        else:
            repeated = 1
    return False


def is_bounded_frontier_url(
    url: str,
    *,
    allowed_hosts: Collection[str] | None = None,
    max_query_params: int = 6,
    max_query_length: int = 512,
    max_path_segments: int = 32,
    max_repeated_path_segment: int = 3,
) -> bool:
    """Return whether ``url`` is safe to add to the HTML crawl frontier."""

    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False
    if not is_crawlable_url(url):
        return False
    if is_frontier_trap_url(
        url,
        max_query_params=max_query_params,
        max_query_length=max_query_length,
        max_path_segments=max_path_segments,
        max_repeated_path_segment=max_repeated_path_segment,
    ):
        return False
    if allowed_hosts is None:
        return True
    host = (parsed.hostname or "").casefold().rstrip(".")
    return any(
        host == value.casefold().rstrip(".")
        or host.endswith(f".{value.casefold().rstrip('.')}")
        for value in allowed_hosts
        if value.strip()
    )


def canonicalize_frontier_url(url: str) -> str:
    """Canonicalize an official URL, including deterministic query ordering."""

    parsed = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            (parsed.hostname or "").casefold().rstrip("."),
            parsed.path or "/",
            query,
            "",
        )
    )


def is_crawlable_url(url: str) -> bool:
    """Return whether the HTML crawler can fetch and parse ``url``."""

    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False
    if parsed.fragment:
        return False
    path = parsed.path.casefold()
    return not any(path.endswith(suffix) for suffix in NON_HTML_RESOURCE_SUFFIXES)
