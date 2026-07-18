from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import time
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_RETRIEVAL_QUERIES = 5


def normalize_search_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def search_query_key(value: str) -> str:
    return "".join(normalize_search_query(value).casefold().split())


class SearchDeadlineExceeded(Exception):
    """網站搜尋已耗盡單一查詢的共用時間額度。"""


@dataclass(frozen=True, slots=True)
class SearchDeadline:
    expires_at: float
    _clock: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )

    @classmethod
    def after(
        cls,
        seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> "SearchDeadline":
        if seconds <= 0:
            raise ValueError("搜尋 deadline 必須大於零")
        return cls(clock() + seconds, clock)

    def remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - self._clock())

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0.0

    def raise_if_expired(self) -> None:
        if self.expired():
            raise SearchDeadlineExceeded("網站搜尋時間額度已耗盡")


class SearchPlan(BaseModel):
    """由工具呼叫一次產生、可直接供資料庫與官網檢索使用的搜尋計畫。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=500)
    search_queries: list[str] = Field(min_length=1, max_length=4)
    concepts: list[str] = Field(min_length=1, max_length=8)
    limit: int = Field(ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = normalize_search_query(value)
        if not normalized:
            raise ValueError("獨立搜尋問題不得為空")
        return normalized

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
            item = normalize_search_query(value)
            if not item or len(item) > max_length:
                raise ValueError(f"{field_name}不得為空且長度不可超過 {max_length}")
            key = search_query_key(item)
            if key not in seen:
                normalized.append(item)
                seen.add(key)
        if not normalized:
            raise ValueError(f"至少需要一個{field_name}")
        return normalized

    @classmethod
    def from_query(cls, query: str, *, limit: int = 6) -> "SearchPlan":
        normalized = normalize_search_query(query)
        return cls(
            query=normalized,
            search_queries=[normalized],
            concepts=[normalized],
            limit=limit,
        )

    @property
    def cache_key(self) -> tuple[str, tuple[str, ...], tuple[str, ...], int]:
        return (
            search_query_key(self.query),
            tuple(search_query_key(value) for value in self.search_queries),
            tuple(search_query_key(value) for value in self.concepts),
            self.limit,
        )

    @property
    def retrieval_queries(self) -> tuple[str, ...]:
        queries: list[str] = []
        seen: set[str] = set()
        for value in (self.query, *self.search_queries):
            normalized = normalize_search_query(value)
            key = search_query_key(normalized)
            if normalized and key not in seen:
                queries.append(normalized)
                seen.add(key)
            if len(queries) >= MAX_RETRIEVAL_QUERIES:
                break
        return tuple(queries)

    @property
    def semantic_text(self) -> str:
        return "\n".join((*self.retrieval_queries, *self.concepts))


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
    relevant_fetch_failure_count: int = 0
    unrelated_fetch_failure_count: int = 0
    timed_out_candidate_count: int = 0
    skipped_candidate_count: int = 0
    query_timed_out: bool = False
    highest_success_score: float | None = None
    highest_fetch_failure_score: float | None = None
    highest_unattempted_score: float | None = None

    @property
    def relevant_failure_count(self) -> int:
        return self.relevant_fetch_failure_count

    @property
    def unrelated_failure_count(self) -> int:
        return self.unrelated_fetch_failure_count

    @property
    def highest_failed_score(self) -> float | None:
        return self.highest_fetch_failure_score

    @property
    def failed_count(self) -> int:
        return self.relevant_fetch_failure_count + self.unrelated_fetch_failure_count

    @property
    def query_relevant_failed_count(self) -> int:
        return self.relevant_fetch_failure_count

    @property
    def visited_count(self) -> int:
        return self.fetched_count + self.failed_count
