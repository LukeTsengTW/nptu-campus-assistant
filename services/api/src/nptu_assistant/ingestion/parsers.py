from __future__ import annotations

from pathlib import Path

import fitz

from nptu_assistant.ingestion.cleaning import extract_clean_html, normalize_text


SUPPORTED_EXTENSIONS = {".pdf", ".html", ".htm", ".md", ".markdown", ".txt"}


def parse_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支援的文件格式：{suffix}")
    if suffix == ".pdf":
        with fitz.open(path) as document:
            return normalize_text("\n".join(page.get_text("text") for page in document))
    text = path.read_text(encoding="utf-8")
    if suffix in {".html", ".htm"}:
        return extract_clean_html(text)
    return normalize_text(text)
