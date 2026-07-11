from __future__ import annotations

from typing import Protocol

from nptu_assistant.rag.models import Evidence, GeneratedAnswer


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LlmProvider(Protocol):
    def generate(self, question: str, evidence: list[Evidence]) -> GeneratedAnswer: ...
