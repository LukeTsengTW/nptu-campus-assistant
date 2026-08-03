from __future__ import annotations

import inspect
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from nptu_assistant.api.schemas import AnswerType
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
    ScopedAnnouncementIngestionResult,
    SitePageIngestionResult,
)
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchExecutionLimits,
    SearchPlan,
)
from nptu_assistant.crawlers.unit_intents import (
    UnitQueryIntent,
    classify_unit_query,
    extract_announcement_topic,
)
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.embedding_cache import RetrievalExecutionContext
from nptu_assistant.rag.completeness import (
    CompletenessAction,
    CompletenessDecision,
    CompletenessFacts,
    CompletenessMode,
    DbFirstCompletenessPolicy,
    QueryIntent,
)
from nptu_assistant.rag.completeness_refresh import (
    CompletenessRefreshScheduler,
    RefreshScheduleResult,
)


logger = logging.getLogger(__name__)

DB_REFRESH_SCHEDULED_WARNING = "以下內容來自已收錄的官方資料，背景更新已排程。"


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
    def ingest(
        self,
        query: str,
        *,
        max_items: int,
        deadline: SearchDeadline | None = None,
        limits: SearchExecutionLimits | None = None,
    ) -> KeywordIngestionResult:
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
        limits: SearchExecutionLimits | None = None,
    ) -> SitePageIngestionResult:
        raise NotImplementedError

    def search_unit_announcements(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope,
        max_items: int,
        deadline: SearchDeadline,
        sort: object = "newest",
        topic: str | None = None,
        limits: SearchExecutionLimits | None = None,
    ) -> ScopedAnnouncementIngestionResult:
        raise NotImplementedError


class CompletenessFactsProvider(Protocol):
    def document_facts(
        self,
        evidence: Collection[Evidence],
        *,
        scope: DocumentSearchScope | None,
        now: datetime,
        strong_score: float,
        min_content_chars: int,
        soft_stale: timedelta,
        hard_stale: timedelta,
        deadline: SearchDeadline | None = None,
    ) -> CompletenessFacts: ...

    def announcement_facts(
        self,
        evidence: Collection[Evidence],
        *,
        unit: str | None,
        now: datetime,
        strong_score: float,
        min_content_chars: int,
        soft_stale: timedelta,
        hard_stale: timedelta,
        source_target_limit: int = 20,
        deadline: SearchDeadline | None = None,
    ) -> CompletenessFacts: ...


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
        completeness_policy: DbFirstCompletenessPolicy | None = None,
        completeness_facts: CompletenessFactsProvider | None = None,
        refresh_scheduler: CompletenessRefreshScheduler | None = None,
        live_fallback_limits: SearchExecutionLimits | None = None,
        live_fallback_max_seconds: float | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._retriever = retriever
        self._refresher = refresher
        self._keyword_ingestor = keyword_ingestor
        self._unit_resolver = unit_resolver
        self._site_page_ingestor = site_page_ingestor
        self._completeness_policy = completeness_policy
        self._completeness_facts = completeness_facts
        self._refresh_scheduler = refresh_scheduler
        self._live_fallback_limits = live_fallback_limits
        self._live_fallback_max_seconds = live_fallback_max_seconds
        self._now = now

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

    def _is_enforcing_policy(self) -> bool:
        return bool(
            self._completeness_policy
            and self._completeness_policy.config.rollout_mode
            is CompletenessMode.ENFORCE
        )

    def _is_shadow_policy(self) -> bool:
        return bool(
            self._completeness_policy
            and self._completeness_policy.config.rollout_mode is CompletenessMode.SHADOW
        )

    def _bounded_keyword_ingest(
        self,
        query: str,
        *,
        max_items: int,
        deadline: SearchDeadline,
    ) -> KeywordIngestionResult:
        if self._keyword_ingestor is None:
            raise RuntimeError("keyword announcement ingestor 未設定")
        method = cast(Any, self._keyword_ingestor.ingest)
        kwargs: dict[str, object] = {"max_items": max_items}
        parameters = inspect.signature(method).parameters
        if "deadline" in parameters:
            kwargs["deadline"] = deadline
        if "limits" in parameters and self._live_fallback_limits is not None:
            kwargs["limits"] = self._live_fallback_limits
        return cast(KeywordIngestionResult, method(query, **kwargs))

    def new_deadline(self) -> SearchDeadline | None:
        """Create the single absolute request deadline when live search exists."""

        if self._site_page_ingestor is None:
            return None
        return self._site_page_ingestor.new_deadline()

    @staticmethod
    def _filter_announcement_unit(
        evidence: Collection[Evidence],
        unit: str | None,
    ) -> list[Evidence]:
        if unit is None:
            return list(evidence)
        return [item for item in evidence if item.unit == unit]

    def _fallback_facts(
        self,
        evidence: Collection[Evidence],
        *,
        exact_scope: bool,
    ) -> CompletenessFacts:
        policy = self._completeness_policy
        if policy is None or policy.config.rollout_mode is CompletenessMode.OFF:
            return CompletenessFacts()
        config = policy.config
        scores = sorted((item.score for item in evidence), reverse=True)
        strong = sum(
            item.score >= config.exact_scope_min_score
            and len(item.content.strip()) >= 160
            for item in evidence
        )
        return CompletenessFacts(
            evidence_count=len(evidence),
            unique_url_count=len({item.url for item in evidence}),
            strong_evidence_count=strong,
            top_score=scores[0] if scores else 0.0,
            score_margin=(
                scores[0] - scores[1]
                if len(scores) > 1
                else (scores[0] if scores else 0.0)
            ),
            current_document_count=len(evidence),
            exact_scope_match_count=len(evidence) if exact_scope else 0,
            fresh_count=len(evidence),
            content_hash_in_sync_count=len(evidence),
            source_coverage_ratio=1.0 if evidence else 0.0,
            canonical_urls=tuple(dict.fromkeys(item.url for item in evidence)),
        )

    def _document_completeness_decision(
        self,
        evidence: Collection[Evidence],
        *,
        scope: DocumentSearchScope | None,
        deadline: SearchDeadline,
    ) -> CompletenessDecision | None:
        policy = self._completeness_policy
        if policy is None or policy.config.rollout_mode is CompletenessMode.OFF:
            return None
        started_at = time.perf_counter()
        config = policy.config
        if self._completeness_facts is None:
            facts = self._fallback_facts(evidence, exact_scope=scope is not None)
        else:
            facts = self._completeness_facts.document_facts(
                evidence,
                scope=scope,
                now=self._now(),
                strong_score=config.exact_scope_min_score,
                min_content_chars=160,
                soft_stale=timedelta(minutes=config.document_soft_stale_minutes),
                hard_stale=timedelta(minutes=config.document_hard_stale_minutes),
                deadline=deadline,
            )
        return self._record_completeness_decision(
            policy.decide(
                facts=facts,
                intent=(
                    QueryIntent.SCOPED_TOPIC if scope is not None else QueryIntent.TOPIC
                ),
                remaining_deadline_seconds=deadline.remaining_seconds(),
            ),
            facts=facts,
            query_kind="document",
            query_intent="scoped" if scope is not None else "topic",
            canonical_unit=scope.canonical_unit if scope is not None else None,
            policy_duration_ms=(time.perf_counter() - started_at) * 1_000,
        )

    def _announcement_completeness_decision(
        self,
        evidence: Collection[Evidence],
        *,
        unit: str | None,
        intent: QueryIntent,
        deadline: SearchDeadline | None,
    ) -> CompletenessDecision | None:
        policy = self._completeness_policy
        if policy is None or policy.config.rollout_mode is CompletenessMode.OFF:
            return None
        started_at = time.perf_counter()
        config = policy.config
        if self._completeness_facts is None:
            facts = self._fallback_facts(evidence, exact_scope=unit is not None)
        else:
            facts = self._completeness_facts.announcement_facts(
                evidence,
                unit=unit,
                now=self._now(),
                strong_score=config.exact_scope_min_score,
                min_content_chars=40,
                soft_stale=timedelta(minutes=config.announcement_soft_stale_minutes),
                hard_stale=timedelta(minutes=config.announcement_hard_stale_minutes),
                deadline=deadline,
            )
        return self._record_completeness_decision(
            policy.decide(
                facts=facts,
                intent=intent,
                remaining_deadline_seconds=(
                    deadline.remaining_seconds() if deadline is not None else 0.0
                ),
            ),
            facts=facts,
            query_kind="announcement",
            query_intent=intent.value,
            canonical_unit=unit,
            policy_duration_ms=(time.perf_counter() - started_at) * 1_000,
        )

    def _record_completeness_decision(
        self,
        decision: CompletenessDecision,
        *,
        facts: CompletenessFacts,
        query_kind: str,
        query_intent: str,
        canonical_unit: str | None,
        policy_duration_ms: float,
    ) -> CompletenessDecision:
        logger.info(
            "db_first_completeness_decision",
            extra={
                "query_kind": query_kind,
                "query_intent": query_intent,
                "scope_type": "unit" if canonical_unit else "global",
                "canonical_unit": canonical_unit,
                "decision_action": decision.action.value,
                "decision_reason_codes": list(decision.reason_codes),
                "evidence_count": facts.evidence_count,
                "strong_evidence_count": facts.strong_evidence_count,
                "top_score": round(facts.top_score, 4),
                "score_margin": round(facts.score_margin, 4),
                "fresh_count": facts.fresh_count,
                "soft_stale_count": facts.soft_stale_count,
                "hard_stale_count": facts.hard_stale_count,
                "pending_ingestion_count": facts.pending_ingestion_count,
                "failed_ingestion_count": facts.failed_ingestion_count,
                "incomplete_announcement_count": facts.incomplete_announcement_count,
                "coverage_ratio": round(facts.source_coverage_ratio, 4),
                "active_refresh_count": facts.active_refresh_count,
                "live_fallback_attempted": False,
                "live_fallback_skipped_reason": (
                    None
                    if decision.action is CompletenessAction.USE_BOUNDED_LIVE_FALLBACK
                    else decision.reason_codes[0]
                    if decision.reason_codes
                    else "policy"
                ),
                "refresh_scheduled": False,
                "policy_duration_ms": round(policy_duration_ms, 3),
            },
        )
        return decision

    def _record_completeness_outcome(
        self,
        decision: CompletenessDecision,
        *,
        live_fallback_attempted: bool,
        live_fallback_skipped_reason: str | None = None,
        refresh_scheduled: bool = False,
    ) -> None:
        logger.info(
            "db_first_completeness_outcome",
            extra={
                "decision_action": decision.action.value,
                "decision_reason_codes": list(decision.reason_codes),
                "live_fallback_attempted": live_fallback_attempted,
                "live_fallback_skipped_reason": live_fallback_skipped_reason,
                "refresh_scheduled": refresh_scheduled,
            },
        )

    def _schedule_refresh(
        self,
        decision: CompletenessDecision,
        *,
        unit: str | None,
        deadline: SearchDeadline | None,
    ) -> RefreshScheduleResult | None:
        if self._refresh_scheduler is None:
            return None
        result = self._refresh_scheduler.schedule(
            urls=decision.schedule_targets,
            source_names=decision.schedule_source_names,
            unit=unit,
            reason=decision.reason_codes[0] if decision.reason_codes else "unknown",
            deadline=deadline,
        )
        logger.info(
            "db_first_refresh_schedule",
            extra={
                "refresh_schedule_attempted": result.attempted,
                "refresh_schedule_succeeded": result.succeeded,
                "refresh_schedule_target_count": result.target_count,
                "refresh_schedule_scheduled_count": result.scheduled_count,
                "refresh_schedule_reason": result.reason,
            },
        )
        return result

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
        *,
        deadline: SearchDeadline | None = None,
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
        if generic_latest:
            if parsed.sort is AnnouncementSort.RELEVANCE:
                parsed = parsed.model_copy(update={"sort": AnnouncementSort.NEWEST})
        arguments = parsed.model_dump()
        arguments["canonical_urls"] = None
        warning: str | None = None
        canonical_unit = resolution.canonical_unit if resolution is not None else None
        if canonical_unit is not None:
            arguments["unit"] = canonical_unit
        announcement_deadline = deadline or self.new_deadline()
        announcement_intent = (
            QueryIntent.LATEST
            if generic_latest
            else QueryIntent.KEYWORD_ANNOUNCEMENT
            if parsed.query and resolution is None
            else QueryIntent.ANNOUNCEMENT
        )
        decision: CompletenessDecision | None = None
        cached_for_policy: list[Evidence] = []
        bounded_fallback = False
        live_deadline: SearchDeadline | None = None
        live_max_items = parsed.limit
        if (
            policy := self._completeness_policy
        ) is not None and policy.config.rollout_mode is not CompletenessMode.OFF:
            cached_for_policy = self._retriever.search_announcements(**arguments)
            if canonical_unit is not None:
                cached_for_policy = [
                    item for item in cached_for_policy if item.unit == canonical_unit
                ]
            decision = self._announcement_completeness_decision(
                cached_for_policy,
                unit=canonical_unit,
                intent=announcement_intent,
                deadline=announcement_deadline,
            )
            if self._is_enforcing_policy() and decision is not None:
                if decision.action is CompletenessAction.USE_DB:
                    self._record_completeness_outcome(
                        decision,
                        live_fallback_attempted=False,
                        live_fallback_skipped_reason="sufficient_database_evidence",
                    )
                    return cached_for_policy, None
                if decision.action is CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH:
                    scheduled = self._schedule_refresh(
                        decision,
                        unit=canonical_unit,
                        deadline=announcement_deadline,
                    )
                    self._record_completeness_outcome(
                        decision,
                        live_fallback_attempted=False,
                        live_fallback_skipped_reason="background_refresh",
                        refresh_scheduled=bool(scheduled and scheduled.succeeded),
                    )
                    return (
                        cached_for_policy,
                        (
                            DB_REFRESH_SCHEDULED_WARNING
                            if scheduled is None or scheduled.succeeded
                            else SITE_SEARCH_PARTIAL_WARNING
                        ),
                    )
                if decision.action is CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING:
                    self._record_completeness_outcome(
                        decision,
                        live_fallback_attempted=False,
                        live_fallback_skipped_reason=decision.reason_codes[0],
                    )
                    return cached_for_policy, SITE_SEARCH_PARTIAL_WARNING
                if announcement_deadline is None:
                    self._record_completeness_outcome(
                        decision,
                        live_fallback_attempted=False,
                        live_fallback_skipped_reason="missing_shared_deadline",
                    )
                    return cached_for_policy, SITE_SEARCH_FAILURE_WARNING
                bounded_fallback = True
                live_deadline = announcement_deadline.capped(
                    self._live_fallback_max_seconds
                    if self._live_fallback_max_seconds is not None
                    else announcement_deadline.remaining_seconds()
                )
                config = policy.config
                live_max_items = min(
                    parsed.limit,
                    config.live_fallback_max_details,
                )
            elif self._is_shadow_policy() and decision is not None:
                logger.info(
                    "db_first_completeness_shadow",
                    extra={
                        "query_kind": "announcement",
                        "query_intent": announcement_intent.value,
                        "legacy_would_live": bool(
                            self._refresher is not None
                            or self._keyword_ingestor is not None
                            or self._site_page_ingestor is not None
                        ),
                        "policy_would_live": (
                            decision.action
                            is CompletenessAction.USE_BOUNDED_LIVE_FALLBACK
                        ),
                        "decision_disagrees": (
                            decision.action
                            is not CompletenessAction.USE_BOUNDED_LIVE_FALLBACK
                        ),
                        "decision_reason_codes": list(decision.reason_codes),
                    },
                )
        if bounded_fallback and (
            resolution is None
            or resolution.status is not UnitResolutionStatus.KNOWN_WITH_SCOPED_SEARCH
        ):
            if self._keyword_ingestor is None or live_deadline is None:
                assert decision is not None
                self._record_completeness_outcome(
                    decision,
                    live_fallback_attempted=False,
                    live_fallback_skipped_reason="bounded_keyword_fallback_unavailable",
                )
                return cached_for_policy, SITE_SEARCH_FAILURE_WARNING
            try:
                query = parsed.query or "公告"
                ingestion = self._bounded_keyword_ingest(
                    query,
                    max_items=live_max_items,
                    deadline=live_deadline,
                )
                arguments["query"] = ingestion.retrieval_query
                arguments["canonical_urls"] = ingestion.canonical_urls
                evidence = self._filter_announcement_unit(
                    self._retriever.search_announcements(**arguments),
                    canonical_unit,
                )
                assert decision is not None
                self._record_completeness_outcome(
                    decision,
                    live_fallback_attempted=True,
                )
                return evidence or cached_for_policy, ingestion.warning
            except Exception:
                assert decision is not None
                self._record_completeness_outcome(
                    decision,
                    live_fallback_attempted=True,
                    live_fallback_skipped_reason="bounded_keyword_fallback_failed",
                )
                return cached_for_policy, SITE_SEARCH_FAILURE_WARNING
        if resolution is not None:
            canonical_unit = resolution.canonical_unit or ""
            if resolution.status is UnitResolutionStatus.KNOWN_WITH_SCOPED_SEARCH:
                official_unit = resolution.official_unit
                if official_unit is None or directory is None:
                    raise UnitResolutionError(
                        "unsupported_unit_source",
                        f"目前無法查詢「{canonical_unit}」的官方公告來源。",
                    )
                scope = directory.scope_for(official_unit)
                scoped_ingestion: ScopedAnnouncementIngestionResult | None = None
                scoped_search_completed = False
                if self._site_page_ingestor is not None:
                    # ChatService passes one absolute deadline to every tool
                    # round.  Shadow/off preserve their legacy candidate
                    # selection but must never mint another full request
                    # budget for a scoped live search.
                    deadline = (
                        live_deadline
                        if bounded_fallback and live_deadline is not None
                        else announcement_deadline
                        or self._site_page_ingestor.new_deadline()
                    )
                    max_items = live_max_items if bounded_fallback else parsed.limit
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
                        method = cast(
                            Any,
                            self._site_page_ingestor.search_unit_announcements,
                        )
                        scoped_kwargs: dict[str, object] = {
                            "scope": scope,
                            "max_items": max_items,
                            "deadline": deadline,
                            "sort": parsed.sort,
                            "topic": parsed.query,
                        }
                        if (
                            bounded_fallback
                            and self._live_fallback_limits is not None
                            and "limits" in inspect.signature(method).parameters
                        ):
                            scoped_kwargs["limits"] = self._live_fallback_limits
                        scoped_ingestion = method(
                            SearchPlan.from_query(search_text, limit=max_items),
                            **scoped_kwargs,
                        )
                        scoped_search_completed = True
                    except Exception:
                        logger.exception(
                            "單位 scoped 公告搜尋失敗",
                            extra={"unit": canonical_unit},
                        )
                if scoped_ingestion is not None and scoped_ingestion.canonical_urls:
                    persisted_arguments = {
                        **arguments,
                        "canonical_urls": scoped_ingestion.canonical_urls,
                    }
                    evidence = self._retriever.search_announcements(
                        **persisted_arguments
                    )
                    evidence = [
                        item for item in evidence if item.unit == canonical_unit
                    ]
                    if decision is not None and bounded_fallback:
                        self._record_completeness_outcome(
                            decision,
                            live_fallback_attempted=True,
                        )
                    return evidence, (
                        scoped_ingestion.warning
                        if evidence
                        else SITE_SEARCH_FAILURE_WARNING
                    )
                cached = self._retriever.search_announcements(**arguments)
                cached = [
                    replace(item, unit=canonical_unit)
                    for item in cached
                    if item.unit == canonical_unit
                ]
                if scoped_ingestion is not None:
                    if decision is not None and bounded_fallback:
                        self._record_completeness_outcome(
                            decision,
                            live_fallback_attempted=True,
                            live_fallback_skipped_reason="bounded_scoped_fallback_incomplete",
                        )
                    return cached, scoped_ingestion.warning
                if decision is not None and bounded_fallback:
                    self._record_completeness_outcome(
                        decision,
                        live_fallback_attempted=True,
                        live_fallback_skipped_reason="bounded_scoped_fallback_failed",
                    )
                return cached, (
                    None if scoped_search_completed else SITE_SEARCH_FAILURE_WARNING
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
        *,
        deadline: SearchDeadline | None = None,
    ) -> tuple[list[Evidence], str | None]:
        execution_context = RetrievalExecutionContext()

        def retrieve(**kwargs: object) -> list[Evidence]:
            method = cast(Any, self._retriever.search_documents_with_plan)
            if "execution_context" in inspect.signature(method).parameters:
                kwargs["execution_context"] = execution_context
            return method(**kwargs)

        def ingest(**kwargs: object) -> SitePageIngestionResult:
            if self._site_page_ingestor is None:
                raise RuntimeError("site page ingestor 未設定")
            method = cast(Any, self._site_page_ingestor.ingest)
            if "execution_context" in inspect.signature(method).parameters:
                kwargs["execution_context"] = execution_context
            return method(**kwargs)

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
                evidence = retrieve(
                    plan=parsed,
                    limit=parsed.limit,
                )
            else:
                evidence = retrieve(
                    plan=parsed,
                    limit=parsed.limit,
                    scope=scope,
                )
            return (
                evidence,
                None,
            )
        deadline = deadline or self._site_page_ingestor.new_deadline()
        try:
            if scope is None:
                cached = retrieve(
                    plan=parsed,
                    limit=parsed.limit,
                    deadline=deadline,
                )
            else:
                cached = retrieve(
                    plan=parsed,
                    limit=parsed.limit,
                    deadline=deadline,
                    scope=scope,
                )
        except SearchDeadlineExceeded:
            return [], SITE_SEARCH_FAILURE_WARNING
        decision = self._document_completeness_decision(
            cached,
            scope=scope,
            deadline=deadline,
        )
        live_limits: SearchExecutionLimits | None = None
        if self._is_enforcing_policy() and decision is not None:
            if decision.action is CompletenessAction.USE_DB:
                self._record_completeness_outcome(
                    decision,
                    live_fallback_attempted=False,
                    live_fallback_skipped_reason="sufficient_database_evidence",
                )
                return cached, None
            if decision.action is CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH:
                scheduled = self._schedule_refresh(
                    decision,
                    unit=scope.canonical_unit if scope is not None else None,
                    deadline=deadline,
                )
                self._record_completeness_outcome(
                    decision,
                    live_fallback_attempted=False,
                    live_fallback_skipped_reason="background_refresh",
                    refresh_scheduled=bool(scheduled and scheduled.succeeded),
                )
                return (
                    cached,
                    (
                        DB_REFRESH_SCHEDULED_WARNING
                        if scheduled is None or scheduled.succeeded
                        else SITE_SEARCH_PARTIAL_WARNING
                    ),
                )
            if decision.action is CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING:
                self._record_completeness_outcome(
                    decision,
                    live_fallback_attempted=False,
                    live_fallback_skipped_reason=decision.reason_codes[0],
                )
                return cached, self._document_search_fallback_warning(cached)
            if decision.action is CompletenessAction.USE_BOUNDED_LIVE_FALLBACK:
                live_limits = self._live_fallback_limits
                if self._live_fallback_max_seconds is not None:
                    deadline = deadline.capped(self._live_fallback_max_seconds)
        elif self._is_shadow_policy() and decision is not None:
            legacy_would_live = self._site_page_ingestor.should_search_live(cached)
            policy_would_live = (
                decision.action is CompletenessAction.USE_BOUNDED_LIVE_FALLBACK
            )
            logger.info(
                "db_first_completeness_shadow",
                extra={
                    "query_kind": "document",
                    "query_intent": "scoped" if scope is not None else "topic",
                    "legacy_would_live": legacy_would_live,
                    "policy_would_live": policy_would_live,
                    "decision_disagrees": legacy_would_live is not policy_would_live,
                    "decision_reason_codes": list(decision.reason_codes),
                },
            )
        if live_limits is None and not self._site_page_ingestor.should_search_live(
            cached
        ):
            return cached, None
        if deadline.expired():
            if decision is not None and live_limits is not None:
                self._record_completeness_outcome(
                    decision,
                    live_fallback_attempted=False,
                    live_fallback_skipped_reason="deadline_expired_before_fallback",
                )
            return cached, self._document_search_fallback_warning(cached)
        try:
            ingest_arguments: dict[str, object] = {
                "plan": parsed,
                "max_items": parsed.limit,
                "deadline": deadline,
                "scope": scope,
            }
            if live_limits is not None:
                ingest_arguments["limits"] = live_limits
            ingestion = ingest(**ingest_arguments)
        except Exception:
            if decision is not None and live_limits is not None:
                self._record_completeness_outcome(
                    decision,
                    live_fallback_attempted=True,
                    live_fallback_skipped_reason="bounded_document_fallback_failed",
                )
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
                refreshed = retrieve(
                    plan=parsed,
                    limit=parsed.limit,
                    deadline=deadline,
                )
            else:
                refreshed = retrieve(
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
        if decision is not None and live_limits is not None:
            self._record_completeness_outcome(
                decision,
                live_fallback_attempted=True,
                live_fallback_skipped_reason=(
                    None
                    if refreshed
                    else "bounded_document_fallback_no_persisted_result"
                ),
            )
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

    def execute(
        self,
        name: str,
        arguments: str,
        *,
        deadline: SearchDeadline | None = None,
    ) -> ToolExecutionResult:
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
                evidence, refresh_warning = self._search_announcements(
                    parsed,
                    deadline=deadline,
                )
                content_limit = 2_000
            elif isinstance(parsed, SearchDocumentsArguments):
                evidence, refresh_warning = self._search_documents(
                    parsed,
                    deadline=deadline,
                )
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
