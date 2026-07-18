from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.crawlers.adapters.nptu_search import AnnouncementSearchResult
from nptu_assistant.crawlers.official_units import (
    DocumentSearchScope,
    ResolvedOfficialUnit,
)
from nptu_assistant.crawlers.refresh import REFRESH_FAILURE_WARNING, RefreshResult
from nptu_assistant.crawlers.resolution import (
    UnitResolution,
    UnitResolutionStatus,
    UnitSourceResolver,
)
from nptu_assistant.crawlers.search import (
    FULL_SEARCH_FAILURE_WARNING,
    KeywordIngestionResult,
)
from nptu_assistant.crawlers.site_search import (
    SITE_SEARCH_FAILURE_WARNING,
    SITE_SEARCH_PARTIAL_WARNING,
    ScoredEvidence,
    SitePageIngestionResult,
)
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchPlan,
)
from nptu_assistant.crawlers.unit_intents import (
    UnitQueryIntent,
    classify_unit_query,
    extract_announcement_topic,
)
from nptu_assistant.rag.models import Evidence


logger = logging.getLogger(__name__)


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

    def search_documents_with_plan(
        self,
        *,
        plan: SearchPlan,
        limit: int,
        deadline: SearchDeadline | None = None,
        scope: DocumentSearchScope | None = None,
    ) -> list[Evidence]: ...

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
    def new_deadline(self) -> SearchDeadline:
        raise NotImplementedError

    def should_search_live(self, evidence: Collection[ScoredEvidence]) -> bool:
        raise NotImplementedError

    def ingest(
        self,
        plan: SearchPlan,
        *,
        max_items: int,
        deadline: SearchDeadline,
        scope: DocumentSearchScope | None = None,
    ) -> SitePageIngestionResult:
        raise NotImplementedError

    def search_unit_announcements(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope,
        max_items: int,
        deadline: SearchDeadline,
    ) -> tuple[tuple[AnnouncementSearchResult, ...], str | None]:
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

    def _resolve_unit(
        self,
        parsed: SearchAnnouncementsArguments,
    ) -> UnitResolution | None:
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
        if resolution.status is UnitResolutionStatus.KNOWN_WITHOUT_VERIFIED_SITE:
            reason = (
                resolution.official_unit.unsupported_reason
                if resolution.official_unit is not None
                else None
            )
            raise UnitResolutionError(
                "unsupported_unit_source",
                f"目前尚未設定「{resolution.canonical_unit}」可驗證的官方公告來源。"
                + (f"原因：{reason}" if reason else ""),
            )
        if resolution.canonical_unit is None:
            raise UnitResolutionError(
                "unknown_unit",
                "無法確認單位的官方公告來源，請提供完整單位名稱。",
            )
        return resolution

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
        resolution = self._resolve_unit(parsed)
        generic_latest = False
        directory = (
            self._unit_resolver.official_units
            if self._unit_resolver is not None
            else None
        )
        if (
            parsed.query
            and directory is not None
            and (resolution is None or resolution.official_unit is not None)
        ):
            topic = (
                extract_announcement_topic(parsed.query, directory)
                if classify_unit_query(parsed.query) is UnitQueryIntent.ANNOUNCEMENT
                else parsed.query
            )
            if topic != parsed.query:
                parsed = parsed.model_copy(update={"query": topic})
                generic_latest = topic is None
        if _is_generic_announcement_query(parsed.query):
            parsed = parsed.model_copy(update={"query": None})
            generic_latest = True
        if generic_latest or resolution is not None:
            if parsed.sort is AnnouncementSort.RELEVANCE:
                parsed = parsed.model_copy(update={"sort": AnnouncementSort.NEWEST})
        arguments = parsed.model_dump()
        arguments["canonical_urls"] = None
        warning: str | None = None
        if resolution is not None:
            canonical_unit = resolution.canonical_unit or ""
            arguments["unit"] = canonical_unit
            if resolution.status is UnitResolutionStatus.KNOWN_WITH_SCOPED_SEARCH:
                official_unit = resolution.official_unit
                if official_unit is None or directory is None:
                    raise UnitResolutionError(
                        "unsupported_unit_source",
                        f"目前無法查詢「{canonical_unit}」的官方公告來源。",
                    )
                scope = directory.scope_for(official_unit)
                live_results: tuple[AnnouncementSearchResult, ...] = ()
                live_warning: str | None = None
                scoped_search_completed = False
                if self._site_page_ingestor is not None:
                    deadline = self._site_page_ingestor.new_deadline()
                    search_text = " ".join(
                        value
                        for value in (
                            canonical_unit,
                            parsed.query,
                            "最新 公告 消息",
                        )
                        if value
                    )
                    try:
                        live_results, live_warning = (
                            self._site_page_ingestor.search_unit_announcements(
                                SearchPlan.from_query(search_text, limit=parsed.limit),
                                scope=scope,
                                max_items=parsed.limit,
                                deadline=deadline,
                            )
                        )
                        scoped_search_completed = True
                    except Exception:
                        logger.exception(
                            "單位 scoped 公告搜尋失敗",
                            extra={"unit": canonical_unit},
                        )
                        live_warning = SITE_SEARCH_FAILURE_WARNING
                if live_results:
                    evidence = [
                        Evidence(
                            id=str(
                                uuid.uuid5(
                                    uuid.NAMESPACE_URL,
                                    item.canonical_url,
                                )
                            ),
                            kind=AnswerType.ANNOUNCEMENT,
                            title=item.title,
                            url=item.canonical_url,
                            unit=canonical_unit,
                            published_at=item.published_at,
                            content=item.body,
                            score=max(0.65, 1.0 - index * 0.02),
                        )
                        for index, item in enumerate(live_results)
                    ]
                    return evidence, live_warning
                cached = self._retriever.search_announcements(**arguments)
                cached = [
                    replace(item, unit=canonical_unit)
                    for item in cached
                    if item.unit == canonical_unit
                ]
                return cached, (
                    SITE_SEARCH_PARTIAL_WARNING
                    if cached
                    else live_warning
                    or (
                        None if scoped_search_completed else SITE_SEARCH_FAILURE_WARNING
                    )
                )

            source = resolution.source
            if source is None:
                raise UnitResolutionError(
                    "unsupported_unit_source",
                    f"目前無法查詢「{canonical_unit}」的官方公告來源。",
                )
            arguments["canonical_urls"] = ()
            if self._refresher is None:
                warning = REFRESH_FAILURE_WARNING
            else:
                try:
                    refresh = self._refresher.ensure_fresh(source.name)
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
        if resolution is not None:
            evidence = [
                replace(item, unit=resolution.canonical_unit or item.unit)
                for item in evidence
            ]
        return evidence, warning

    def _search_documents(
        self,
        parsed: SearchDocumentsArguments,
    ) -> tuple[list[Evidence], str | None]:
        scope: DocumentSearchScope | None = None
        official_unit: ResolvedOfficialUnit | None = None
        if self._unit_resolver is not None:
            resolution = self._unit_resolver.resolve(None, parsed.query)
            if resolution.status is UnitResolutionStatus.AMBIGUOUS:
                candidates = "、".join(resolution.candidates)
                raise UnitResolutionError(
                    "ambiguous_unit",
                    f"單位名稱可能對應多個單位（{candidates}），請指定完整名稱。",
                )
            official_unit = resolution.official_unit
            directory = self._unit_resolver.official_units
            if official_unit is not None and directory is not None:
                scope = directory.scope_for(official_unit)
        if (
            official_unit is not None
            and official_unit.homepage_url is not None
            and classify_unit_query(parsed.query) is UnitQueryIntent.HOMEPAGE
        ):
            homepage_url = official_unit.homepage_url
            return (
                [
                    Evidence(
                        id=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"nptu-official-unit:{official_unit.canonical_name}:{homepage_url}",
                            )
                        ),
                        kind=AnswerType.OFFICIAL_DOCUMENT,
                        title=f"國立屏東大學{official_unit.canonical_name}官方網站",
                        url=homepage_url,
                        unit=official_unit.canonical_name,
                        published_at=None,
                        content=f"{official_unit.canonical_name}官方網站首頁。",
                        score=1.0,
                    )
                ],
                None,
            )
        if self._site_page_ingestor is None:
            if scope is None:
                evidence = self._retriever.search_documents_with_plan(
                    plan=parsed,
                    limit=parsed.limit,
                )
            else:
                evidence = self._retriever.search_documents_with_plan(
                    plan=parsed,
                    limit=parsed.limit,
                    scope=scope,
                )
            return (
                evidence,
                None,
            )
        deadline = self._site_page_ingestor.new_deadline()
        try:
            if scope is None:
                cached = self._retriever.search_documents_with_plan(
                    plan=parsed,
                    limit=parsed.limit,
                    deadline=deadline,
                )
            else:
                cached = self._retriever.search_documents_with_plan(
                    plan=parsed,
                    limit=parsed.limit,
                    deadline=deadline,
                    scope=scope,
                )
        except SearchDeadlineExceeded:
            return [], SITE_SEARCH_FAILURE_WARNING
        if not self._site_page_ingestor.should_search_live(cached):
            return cached, None
        if deadline.expired():
            return cached, self._document_search_fallback_warning(cached)
        try:
            ingestion = self._site_page_ingestor.ingest(
                parsed,
                max_items=parsed.limit,
                deadline=deadline,
                scope=scope,
            )
        except Exception:
            return cached, self._document_search_fallback_warning(cached)
        if deadline.expired():
            return cached, self._document_search_warning(
                cached=cached,
                final_evidence=cached,
                ingestion=ingestion,
                refreshed_completed=False,
            )
        try:
            if scope is None:
                refreshed = self._retriever.search_documents_with_plan(
                    plan=parsed,
                    limit=parsed.limit,
                    deadline=deadline,
                )
            else:
                refreshed = self._retriever.search_documents_with_plan(
                    plan=parsed,
                    limit=parsed.limit,
                    deadline=deadline,
                    scope=scope,
                )
        except Exception:
            return cached, self._document_search_warning(
                cached=cached,
                final_evidence=cached,
                ingestion=ingestion,
                refreshed_completed=False,
                used_cached_fallback_after_refresh=False,
            )
        final_evidence = refreshed or cached
        return final_evidence, self._document_search_warning(
            cached=cached,
            final_evidence=final_evidence,
            ingestion=ingestion,
            refreshed_completed=True,
            used_cached_fallback_after_refresh=not refreshed and bool(cached),
        )

    @staticmethod
    def _document_search_fallback_warning(
        cached: Collection[Evidence],
    ) -> str:
        return SITE_SEARCH_PARTIAL_WARNING if cached else SITE_SEARCH_FAILURE_WARNING

    @classmethod
    def _document_search_warning(
        cls,
        *,
        cached: Collection[Evidence],
        final_evidence: Collection[Evidence],
        ingestion: SitePageIngestionResult,
        refreshed_completed: bool,
        used_cached_fallback_after_refresh: bool = False,
    ) -> str | None:
        ingestion_incomplete = (
            ingestion.ingestion_timed_out
            or not ingestion.ingestion_complete
            or ingestion.relevant_pages_persisted < ingestion.relevant_pages_found
        )
        if ingestion_incomplete:
            return cls._document_search_fallback_warning(final_evidence)
        if used_cached_fallback_after_refresh:
            return SITE_SEARCH_PARTIAL_WARNING
        if not refreshed_completed and ingestion.relevant_pages_found:
            return cls._document_search_fallback_warning(cached)
        if ingestion.relevant_pages_found and not final_evidence:
            return SITE_SEARCH_FAILURE_WARNING
        return ingestion.warning

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
            elif isinstance(parsed, GetAnnouncementArguments):
                item = self._retriever.get_announcement(parsed.announcement_id)
                evidence = [item] if item else []
                content_limit = 8_000
            else:
                return _error("invalid_tool_arguments", "工具參數格式或範圍不正確。")
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
