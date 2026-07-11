from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup, Tag


_WHITESPACE = re.compile(r"[\t \u3000]+")


def normalize_text(text: str) -> str:
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized = _WHITESPACE.sub(" ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def extract_clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("script", "iframe", "style", "noscript", "nav", "header", "footer", "aside"):
        for node in soup.select(selector):
            node.decompose()
    for node in list(soup.find_all(True)):
        if not isinstance(node, Tag) or node.parent is None:
            continue
        style = str(node.get("style", "")).replace(" ", "").lower()
        if (
            node.has_attr("hidden")
            or str(node.get("aria-hidden", "")).lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            node.decompose()
    root = soup.find("main") or soup.find(class_="meditor") or soup.body or soup
    return normalize_text(root.get_text("\n", strip=True))


def content_hash(text: str) -> str:
    normalized = _WHITESPACE.sub(" ", normalize_text(text)).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
