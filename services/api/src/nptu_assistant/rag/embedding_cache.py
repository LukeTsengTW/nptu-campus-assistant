from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    search_query_key,
)
from nptu_assistant.providers.protocols import EmbeddingProvider


def _provider_key(provider: EmbeddingProvider) -> tuple[object, ...]:
    provider_type = type(provider)
    return (
        id(provider),
        provider_type.__module__,
        provider_type.__qualname__,
        getattr(provider, "provider", None),
        getattr(provider, "_model", None),
        getattr(provider, "model", None),
        getattr(provider, "_dimensions", None),
        getattr(provider, "dimensions", None),
    )


@dataclass(slots=True)
class RetrievalExecutionContext:
    """單一使用者請求內的 query embedding cache。"""

    query_vectors: dict[tuple[tuple[object, ...], str], list[float]] = field(
        default_factory=dict
    )
    _dimensions: dict[tuple[object, ...], int] = field(default_factory=dict)

    def embed(
        self,
        provider: EmbeddingProvider,
        texts: Iterable[str],
        *,
        deadline: SearchDeadline | None = None,
    ) -> list[list[float]]:
        values = list(texts)
        if not values:
            return []
        if deadline is not None:
            deadline.raise_if_expired()
        provider_key = _provider_key(provider)
        normalized: list[tuple[str, str]] = [
            (value, search_query_key(value)) for value in values
        ]
        missing: list[str] = []
        missing_keys: set[tuple[tuple[object, ...], str]] = set()
        for value, key in normalized:
            cache_key = (provider_key, key)
            if cache_key not in self.query_vectors and cache_key not in missing_keys:
                missing.append(value)
                missing_keys.add(cache_key)
        if missing:
            if deadline is not None:
                deadline.raise_if_expired()
            vectors = provider.embed(
                missing,
                timeout_seconds=(
                    deadline.remaining_seconds() if deadline is not None else None
                ),
            )
            if len(vectors) != len(missing):
                raise ValueError("文件查詢與 embedding 數量不一致")
            for value, vector in zip(missing, vectors, strict=True):
                if not vector:
                    raise ValueError("embedding 向量不得為空")
                dimensions = self._dimensions.get(provider_key)
                if dimensions is None:
                    self._dimensions[provider_key] = len(vector)
                elif dimensions != len(vector):
                    raise ValueError("不同 embedding 維度不可共用 request cache")
                self.query_vectors[(provider_key, search_query_key(value))] = list(
                    vector
                )
            if deadline is not None:
                deadline.raise_if_expired()
        if deadline is not None:
            deadline.raise_if_expired()
        result: list[list[float]] = []
        for _value, key in normalized:
            vector = self.query_vectors.get((provider_key, key))
            if vector is None:
                raise ValueError("embedding cache 遺失查詢向量")
            result.append(list(vector))
        return result

    def clear(self) -> None:
        self.query_vectors.clear()
        self._dimensions.clear()
