from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SearchPlan(BaseModel):
    """由工具呼叫一次產生、可直接供資料庫與官網檢索使用的搜尋計畫。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=500)
    search_queries: list[str] = Field(min_length=1, max_length=4)
    concepts: list[str] = Field(min_length=1, max_length=8)
    limit: int = Field(ge=1, le=20)

    @field_validator("search_queries")
    @classmethod
    def validate_search_queries(cls, values: list[str]) -> list[str]:
        return cls._normalize_values(values, max_length=200, field_name="搜尋變體")

    @field_validator("concepts")
    @classmethod
    def validate_concepts(cls, values: list[str]) -> list[str]:
        return cls._normalize_values(values, max_length=80, field_name="搜尋概念")

    @model_validator(mode="after")
    def validate_plan(self) -> "SearchPlan":
        if not self.query.strip():
            raise ValueError("獨立搜尋問題不得為空")
        return self

    @staticmethod
    def _normalize_values(
        values: list[str],
        *,
        max_length: int,
        field_name: str,
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = " ".join(value.split())
            if not item or len(item) > max_length:
                raise ValueError(f"{field_name}不得為空且長度不可超過 {max_length}")
            key = item.casefold()
            if key not in seen:
                normalized.append(item)
                seen.add(key)
        if not normalized:
            raise ValueError(f"至少需要一個{field_name}")
        return normalized

    @classmethod
    def from_query(cls, query: str, *, limit: int = 6) -> "SearchPlan":
        normalized = " ".join(query.split())
        return cls(
            query=normalized,
            search_queries=[normalized],
            concepts=[normalized],
            limit=limit,
        )

    @property
    def cache_key(self) -> tuple[str, tuple[str, ...], tuple[str, ...], int]:
        return (
            self.query.casefold(),
            tuple(value.casefold() for value in self.search_queries),
            tuple(value.casefold() for value in self.concepts),
            self.limit,
        )

    @property
    def semantic_text(self) -> str:
        return "\n".join(
            dict.fromkeys((self.query, *self.search_queries, *self.concepts))
        )


@dataclass(frozen=True, slots=True)
class DiscoveredPage:
    url: str
    label: str = ""
    relevance: float = 0.0


@dataclass(frozen=True, slots=True)
class CandidatePage:
    url: str
    anchor_text: str = ""
    depth: int = 0
    parent_relevance: float = 0.0
    discovery_relevance: float = 0.0


@dataclass(frozen=True, slots=True)
class SearchDiagnostics:
    discovered_count: int = 0
    fetched_count: int = 0
    relevant_success_count: int = 0
    relevant_failure_count: int = 0
    unrelated_failure_count: int = 0
    highest_success_score: float | None = None
    highest_failed_score: float | None = None
