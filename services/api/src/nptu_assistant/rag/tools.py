from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from nptu_assistant.crawlers.refresh import REFRESH_FAILURE_WARNING, RefreshResult
from nptu_assistant.crawlers.resolution import UnitResolutionStatus, UnitSourceResolver
from nptu_assistant.crawlers.search import (
    FULL_SEARCH_FAILURE_WARNING,
    KeywordIngestionResult,
)
from nptu_assistant.crawlers.site_search import (
    SITE_SEARCH_FAILURE_WARNING,
    ScoredEvidence,
    SitePageIngestionResult,
)
from nptu_assistant.crawlers.site_models import SearchPlan
from nptu_assistant.rag.models import Evidence


class AnnouncementSort(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    RELEVANCE = "relevance"


class SearchAnnouncementsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str | None = Field(max_length=500)
    limit: int = Field(ge=1, le=20)
    sort: AnnouncementSort
    unit: str | None = Field(max_length=200)
    date_from: date | None
    date_to: date | None

    @model_validator(mode="after")
    def validate_date_range(self) -> "SearchAnnouncementsArguments":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("起始日期不得晚於結束日期")
        return self


class SearchDocumentsArguments(SearchPlan):
    pass


class GetAnnouncementArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    announcement_id: str = Field(min_length=1, max_length=200)


def tool_definitions() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "name": "search_announcements",
            "description": "搜尋、篩選或列出國立屏東大學公告。",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": ["string", "null"],
                        "description": "公告主題或搜尋文字；單純列出最新或最近公告時必須使用 null，不得填入「最新公告」等意圖文字。",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "sort": {
                        "type": "string",
                        "enum": ["newest", "oldest", "relevance"],
                    },
                    "unit": {"type": ["string", "null"]},
                    "date_from": {
                        "type": ["string", "null"],
                        "description": "起始日期，格式 YYYY-MM-DD。",
                    },
                    "date_to": {
                        "type": ["string", "null"],
                        "description": "結束日期，格式 YYYY-MM-DD。",
                    },
                },
                "required": ["query", "limit", "sort", "unit", "date_from", "date_to"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "search_documents",
            "description": (
                "搜尋國立屏東大學校規、申請流程、校務文件與官方網站頁面。"
                "必須依最近對話產生獨立完整 query、少量語意變體與核心概念；"
                "不得提供 URL，概念不要求全部逐字同時出現。"
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "已解除代名詞與上下文指涉的 standalone query，不含「查詢、幫我找、請問」等操作詞。",
                    },
                    "search_queries": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": "1 到 4 個語意相近但用語不同的官方資料檢索變體。",
                    },
                    "concepts": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                        "description": "1 到 8 個語意概念；不代表頁面必須同時逐字包含所有概念。",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query", "search_queries", "concepts", "limit"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_announcement",
            "description": "依公告 ID 取得一則公告的詳細內容。",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"announcement_id": {"type": "string"}},
                "required": ["announcement_id"],
                "additionalProperties": False,
            },
        },
    ]


class StructuredRetriever(Protocol):
    def search_announcements(
        self,
        *,
        query: str | None,
        limit: int,
        sort: AnnouncementSort,
        unit: str | None,
        date_from: date | None,
        date_to: date | None,
        canonical_urls: tuple[str, ...] | None = None,
    ) -> list[Evidence]: ...

    def search_documents(self, *, query: str, limit: int) -> list[Evidence]: ...

    def get_announcement(self, announcement_id: str) -> Evidence | None: ...


class AnnouncementRefresher(Protocol):
    def ensure_fresh(self, source_name: str) -> RefreshResult:
        raise NotImplementedError


class KeywordAnnouncementIngestor(Protocol):
    def ingest(self, query: str, *, max_items: int) -> KeywordIngestionResult:
        raise NotImplementedError

    def normalize(self, text: str) -> str:
        raise NotImplementedError


class SitePageIngestor(Protocol):
    def should_search_live(self, evidence: Collection[ScoredEvidence]) -> bool:
        raise NotImplementedError

    def ingest(self, plan: SearchPlan, *, max_items: int) -> SitePageIngestionResult:
        raise NotImplementedError


_GENERIC_ANNOUNCEMENT_PHRASES = frozenset(
    {
        "公告",
        "最新公告",
        "最近公告",
        "有哪些公告",
        "最近有哪些公告",
        "最新有哪些公告",
        "有什麼公告",
        "最近有什麼公告",
        "最新有什麼公告",
        "有那些公告",
        "最近有那些公告",
        "最新有那些公告",
        "有哪些最新公告",
        "有什麼最新公告",
        "有那些最新公告",
        "消息",
        "最新消息",
        "最近消息",
        "通知",
        "最新通知",
        "最近通知",
    }
)
_ANNOUNCEMENT_REQUEST_PREFIXES = (
    "想知道",
    "告訴我",
    "幫忙",
    "幫我",
    "查詢",
    "搜尋",
    "搜索",
    "列出",
    "請問",
    "看看",
    "可以",
    "麻煩",
    "請",
    "查",
    "找",
    "列",
)


def _is_generic_announcement_query(query: str | None) -> bool:
    if not query:
        return False
    normalized = re.sub(
        r"[\s\u3000，。！？!?、：:；;「」『』（）()【】\[\]<>〈〉…]+", "", query
    )
    while normalized:
        prefix = next(
            (
                item
                for item in _ANNOUNCEMENT_REQUEST_PREFIXES
                if normalized.startswith(item)
            ),
            None,
        )
        if prefix is None:
            break
        normalized = normalized[len(prefix) :]
    normalized = normalized.removesuffix("一下")
    return normalized in _GENERIC_ANNOUNCEMENT_PHRASES


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    output: str
    evidence: list[Evidence]
    warning: str | None = None


def _serialize_evidence(item: Evidence, *, content_limit: int) -> dict[str, object]:
    return {
        "id": item.id,
        "kind": item.kind.value,
        "title": item.title,
        "url": item.url,
        "unit": item.unit,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "content": item.content[:content_limit],
        "score": item.score,
    }


def _error(code: str, message: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        output=json.dumps(
            {"error": {"code": code, "message": message}}, ensure_ascii=False
        ),
        evidence=[],
    )


class UnitResolutionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolExecutor:
    def __init__(
        self,
        retriever: StructuredRetriever,
        refresher: AnnouncementRefresher | None = None,
        keyword_ingestor: KeywordAnnouncementIngestor | None = None,
        unit_resolver: UnitSourceResolver | None = None,
        site_page_ingestor: SitePageIngestor | None = None,
    ) -> None:
        self._retriever = retriever
        self._refresher = refresher
        self._keyword_ingestor = keyword_ingestor
        self._unit_resolver = unit_resolver
        self._site_page_ingestor = site_page_ingestor

    def _resolve_unit_source(
        self,
        parsed: SearchAnnouncementsArguments,
    ) -> tuple[str, str] | None:
        if self._unit_resolver is None:
            return None
        resolution = self._unit_resolver.resolve(parsed.unit, parsed.query)
        if resolution.status is UnitResolutionStatus.NONE:
            return None
        if resolution.status is UnitResolutionStatus.UNKNOWN:
            raise UnitResolutionError(
                "unknown_unit",
                f"無法辨識「{resolution.requested}」對應的校內單位，請提供正式單位名稱。",
            )
        if resolution.status is UnitResolutionStatus.AMBIGUOUS:
            candidates = "、".join(resolution.candidates)
            raise UnitResolutionError(
                "ambiguous_unit",
                f"單位名稱可能對應多個單位（{candidates}），請指定完整名稱。",
            )
        if resolution.status is UnitResolutionStatus.UNSUPPORTED:
            raise UnitResolutionError(
                "unsupported_unit_source",
                f"目前尚未設定「{resolution.canonical_unit}」的官方公告來源。",
            )
        if resolution.source is None or resolution.canonical_unit is None:
            raise UnitResolutionError(
                "unknown_unit",
                "無法確認單位的官方公告來源，請提供完整單位名稱。",
            )
        return resolution.canonical_unit, resolution.source.name

    def _refresh_overview(
        self, parsed: SearchAnnouncementsArguments
    ) -> RefreshResult | None:
        if parsed.sort is not AnnouncementSort.NEWEST or self._refresher is None:
            return None
        try:
            return self._refresher.ensure_fresh("nptu-overview")
        except Exception:
            return RefreshResult(
                "nptu-overview",
                attempted=True,
                succeeded=False,
                warning=REFRESH_FAILURE_WARNING,
            )

    def _search_announcements(
        self,
        parsed: SearchAnnouncementsArguments,
    ) -> tuple[list[Evidence], str | None]:
        if _is_generic_announcement_query(parsed.query):
            updates: dict[str, object] = {"query": None}
            if parsed.sort is AnnouncementSort.RELEVANCE:
                updates["sort"] = AnnouncementSort.NEWEST
            parsed = parsed.model_copy(update=updates)
        arguments = parsed.model_dump()
        arguments["canonical_urls"] = None
        warning: str | None = None
        resolved_source = self._resolve_unit_source(parsed)
        if resolved_source is not None:
            canonical_unit, source_name = resolved_source
            arguments["unit"] = canonical_unit
            if parsed.sort is AnnouncementSort.RELEVANCE:
                arguments["sort"] = AnnouncementSort.NEWEST
            arguments["canonical_urls"] = ()
            if self._refresher is None:
                warning = REFRESH_FAILURE_WARNING
            else:
                try:
                    refresh = self._refresher.ensure_fresh(source_name)
                    arguments["canonical_urls"] = (
                        () if refresh.canonical_urls is None else refresh.canonical_urls
                    )
                    warning = refresh.warning
                except Exception:
                    warning = REFRESH_FAILURE_WARNING
        elif parsed.query and self._keyword_ingestor is not None:
            try:
                arguments["query"] = self._keyword_ingestor.normalize(parsed.query)
                if parsed.unit:
                    arguments["unit"] = self._keyword_ingestor.normalize(parsed.unit)
                ingestion = self._keyword_ingestor.ingest(
                    parsed.query, max_items=parsed.limit
                )
                arguments["query"] = ingestion.retrieval_query
                arguments["canonical_urls"] = ingestion.canonical_urls
                warning = ingestion.warning
            except Exception:
                warning = FULL_SEARCH_FAILURE_WARNING
        elif not parsed.query:
            overview_refresh = self._refresh_overview(parsed)
            if overview_refresh is not None:
                arguments["canonical_urls"] = (
                    ()
                    if overview_refresh.canonical_urls is None
                    else overview_refresh.canonical_urls
                )
                warning = overview_refresh.warning
        evidence = self._retriever.search_announcements(**arguments)
        if resolved_source is not None:
            evidence = [replace(item, unit=resolved_source[0]) for item in evidence]
        return evidence, warning

    def _search_documents(
        self,
        parsed: SearchDocumentsArguments,
    ) -> tuple[list[Evidence], str | None]:
        cached = self._retriever.search_documents(
            query=parsed.query,
            limit=parsed.limit,
        )
        if (
            self._site_page_ingestor is None
            or not self._site_page_ingestor.should_search_live(cached)
        ):
            return cached, None
        try:
            ingestion = self._site_page_ingestor.ingest(
                parsed,
                max_items=parsed.limit,
            )
        except Exception:
            return cached, SITE_SEARCH_FAILURE_WARNING
        refreshed = self._retriever.search_documents(
            query=parsed.query,
            limit=parsed.limit,
        )
        return refreshed or cached, ingestion.warning

    def execute(self, name: str, arguments: str) -> ToolExecutionResult:
        validators: dict[str, type[BaseModel]] = {
            "search_announcements": SearchAnnouncementsArguments,
            "search_documents": SearchDocumentsArguments,
            "get_announcement": GetAnnouncementArguments,
        }
        validator = validators.get(name)
        if validator is None:
            return _error("unknown_tool", "模型要求了未註冊的工具。")
        try:
            raw = json.loads(arguments)
            if not isinstance(raw, dict):
                raise ValueError("arguments must be an object")
            parsed = validator.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, ValueError):
            return _error("invalid_tool_arguments", "工具參數格式或範圍不正確。")

        refresh_warning: str | None = None
        try:
            if isinstance(parsed, SearchAnnouncementsArguments):
                evidence, refresh_warning = self._search_announcements(parsed)
                content_limit = 2_000
            elif isinstance(parsed, SearchDocumentsArguments):
                evidence, refresh_warning = self._search_documents(parsed)
                content_limit = 2_000
            else:
                item = self._retriever.get_announcement(parsed.announcement_id)
                evidence = [item] if item else []
                content_limit = 8_000
        except UnitResolutionError as exc:
            return _error(exc.code, exc.message)
        except ValueError:
            return _error("invalid_tool_arguments", "工具參數格式或範圍不正確。")
        except Exception:
            return _error("tool_execution_error", "資料查詢暫時無法完成。")

        payload = {
            "results": [
                _serialize_evidence(item, content_limit=content_limit)
                for item in evidence
            ],
            "count": len(evidence),
            "warning": refresh_warning,
        }
        return ToolExecutionResult(
            output=json.dumps(payload, ensure_ascii=False),
            evidence=evidence,
            warning=refresh_warning,
        )
