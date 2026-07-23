from __future__ import annotations

from urllib.parse import urlsplit


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


def is_crawlable_url(url: str) -> bool:
    """Return whether the HTML crawler can fetch and parse ``url``."""

    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False
    if parsed.fragment:
        return False
    path = parsed.path.casefold()
    return not any(path.endswith(suffix) for suffix in NON_HTML_RESOURCE_SUFFIXES)
