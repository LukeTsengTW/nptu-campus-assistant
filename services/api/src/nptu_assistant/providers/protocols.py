from __future__ import annotations

from typing import Protocol

from nptu_assistant.rag.models import ModelTurn


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LlmProvider(Protocol):
    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelTurn: ...
