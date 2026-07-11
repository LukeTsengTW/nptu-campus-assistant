from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken


_HEADING = re.compile(r"(?m)^#{1,6}\s+\S.*$")


@dataclass(frozen=True, slots=True)
class TextChunk:
    sequence: int
    content: str
    token_count: int


def chunk_text(
    text: str,
    *,
    target_tokens: int = 700,
    overlap_tokens: int = 100,
) -> list[TextChunk]:
    if target_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("chunk token 設定無效")
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        encoding = None
    tokens = encoding.encode(text) if encoding else list(text)
    heading_boundaries = [
        len(encoding.encode(text[: match.start()])) if encoding else match.start()
        for match in _HEADING.finditer(text)
    ]
    chunks: list[TextChunk] = []
    start = 0
    sequence = 0
    while start < len(tokens):
        end = min(start + target_tokens, len(tokens))
        if end < len(tokens):
            preferred = [
                boundary
                for boundary in heading_boundaries
                if start + (target_tokens // 2) <= boundary <= end
            ]
            if preferred:
                end = max(preferred)
        current = tokens[start:end]
        content = (encoding.decode(current) if encoding else "".join(current)).strip()
        if content:
            chunks.append(TextChunk(sequence=sequence, content=content, token_count=len(current)))
            sequence += 1
        if end >= len(tokens):
            break
        start = max(start + 1, end - overlap_tokens)
    return chunks
