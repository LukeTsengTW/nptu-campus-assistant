from __future__ import annotations

import hashlib

from nptu_assistant.rag.models import Evidence, GeneratedAnswer


class FakeEmbeddingProvider:
    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append(
                [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(self.dimensions)]
            )
        return vectors


class FakeLlmProvider:
    def generate(self, question: str, evidence: list[Evidence]) -> GeneratedAnswer:
        if not evidence:
            return GeneratedAnswer(answer="目前收錄的官方資料不足以確認。", used_source_ids=[])
        first = evidence[0]
        return GeneratedAnswer(
            answer=f"根據「{first.title}」：{first.content}",
            used_source_ids=[first.id],
        )
