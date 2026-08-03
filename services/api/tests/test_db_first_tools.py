from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from nptu_assistant.api.schemas import AnswerType, CrawlSummary, IngestionSummary
from nptu_assistant.crawlers.config import (
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.official_units import (
    load_official_unit_directory_for_config,
)
from nptu_assistant.crawlers.resolution import UnitSourceResolver
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDiagnostics,
    SearchExecutionLimits,
)
from nptu_assistant.crawlers.site_search import (
    ScopedAnnouncementIngestionResult,
    SitePageIngestionResult,
)
from nptu_assistant.rag.completeness import (
    CompletenessConfig,
    CompletenessFacts,
    CompletenessMode,
    DbFirstCompletenessPolicy,
)
from nptu_assistant.rag.completeness_refresh import CompletenessRefreshScheduler
from nptu_assistant.crawlers.search import KeywordIngestionResult
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.tools import AnnouncementSort, ToolExecutor


def _document() -> Evidence:
    return Evidence(
        id="doc-1",
        kind=AnswerType.OFFICIAL_DOCUMENT,
        title="學貸申請流程",
        url="https://www.nptu.edu.tw/loan",
        unit="學務處",
        published_at=None,
        content="學生申請就學貸款時，請依官方流程準備文件並於期限內送件。" * 8,
        score=0.92,
    )


def _announcement() -> Evidence:
    return Evidence(
        id="announcement-1",
        kind=AnswerType.ANNOUNCEMENT,
        title="獎學金申請公告",
        url="https://www.nptu.edu.tw/announcement/1",
        unit="學務處",
        published_at=date(2026, 8, 1),
        content="獎學金申請日期與資格詳見本公告。" * 4,
        score=0.91,
    )


class _Retriever:
    def __init__(
        self, documents: list[list[Evidence]], announcements: list[Evidence]
    ) -> None:
        self.documents = documents
        self.announcements = announcements
        self.calls: list[str] = []

    def search_documents_with_plan(self, **_kwargs: object) -> list[Evidence]:
        self.calls.append("documents")
        return self.documents.pop(0)

    def search_documents(self, **_kwargs: object) -> list[Evidence]:
        return self.search_documents_with_plan()

    def search_announcements(self, **_kwargs: object) -> list[Evidence]:
        self.calls.append("announcements")
        return list(self.announcements)

    def get_announcement(self, _announcement_id: str) -> Evidence | None:
        return None


class _Facts:
    def __init__(
        self, document: CompletenessFacts, announcement: CompletenessFacts
    ) -> None:
        self.document = document
        self.announcement = announcement

    def document_facts(
        self, _evidence: list[Evidence], **_kwargs: object
    ) -> CompletenessFacts:
        return self.document

    def announcement_facts(
        self, _evidence: list[Evidence], **_kwargs: object
    ) -> CompletenessFacts:
        return self.announcement


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def schedule_pages(self, **kwargs: object) -> int:
        self.calls.append(kwargs)
        return 1

    def schedule_announcement_sources(self, **kwargs: object) -> int:
        self.calls.append({"source": kwargs})
        return 1


class _Ingestor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.deadline = SearchDeadline.after(25.0)

    def new_deadline(self) -> SearchDeadline:
        return self.deadline

    def should_search_live(self, _evidence: list[Evidence]) -> bool:
        return True

    def ingest(self, **kwargs: object) -> SitePageIngestionResult:
        self.calls.append(kwargs)
        return SitePageIngestionResult(
            IngestionSummary(created=1),
            None,
            SearchDiagnostics(relevant_success_count=1),
            relevant_pages_found=1,
            relevant_pages_persisted=1,
        )


class _MustNotRun:
    def __getattr__(self, _name: str) -> object:
        raise AssertionError(
            "DB-first complete request 不得執行 live announcement path"
        )


class _KeywordIngestor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def normalize(self, text: str) -> str:
        return text

    def ingest(
        self,
        query: str,
        *,
        max_items: int,
        deadline: SearchDeadline | None = None,
        limits: SearchExecutionLimits | None = None,
    ) -> KeywordIngestionResult:
        self.calls.append(
            {
                "query": query,
                "max_items": max_items,
                "deadline": deadline,
                "limits": limits,
            }
        )
        return KeywordIngestionResult(query, CrawlSummary(), None, ())


class _ScopedIngestor(_Ingestor):
    def search_unit_announcements(
        self, _plan: object, **_kwargs: object
    ) -> ScopedAnnouncementIngestionResult:
        self.calls.append(_kwargs)
        return ScopedAnnouncementIngestionResult((), None, SearchDiagnostics())


def _policy() -> DbFirstCompletenessPolicy:
    return DbFirstCompletenessPolicy(
        CompletenessConfig(rollout_mode=CompletenessMode.ENFORCE)
    )


def _facts(**changes: object) -> CompletenessFacts:
    values = dict(
        evidence_count=2,
        unique_url_count=2,
        strong_evidence_count=2,
        top_score=0.91,
        score_margin=0.18,
        current_document_count=2,
        fresh_count=2,
        content_hash_in_sync_count=2,
        source_coverage_ratio=1.0,
        canonical_urls=("https://www.nptu.edu.tw/loan",),
    )
    values.update(changes)
    return CompletenessFacts(**values)


def _document_arguments() -> str:
    return json.dumps(
        {
            "query": "學生就學貸款申請流程",
            "search_queries": ["就學貸款申請"],
            "concepts": ["就學貸款", "申請"],
            "limit": 6,
        },
        ensure_ascii=False,
    )


def _scoped_unit_resolver() -> UnitSourceResolver:
    config_path = (
        Path(__file__).resolve().parents[3] / "data" / "sources" / "announcements.yaml"
    )
    keyword_config = load_keyword_search_config(config_path)
    return UnitSourceResolver(
        load_source_configs(config_path),
        keyword_config.aliases,
        keyword_config.source_routes,
        load_official_unit_directory_for_config(config_path),
    )


def test_fresh_document_uses_db_without_http_or_ingestion() -> None:
    document = _document()
    ingestor = _Ingestor()
    executor = ToolExecutor(
        _Retriever([[document]], []),
        site_page_ingestor=ingestor,
        completeness_policy=_policy(),
        completeness_facts=_Facts(_facts(), _facts()),
    )

    result = executor.execute("search_documents", _document_arguments())

    assert result.evidence == [document]
    assert ingestor.calls == []


def test_soft_stale_document_schedules_durably_without_live_ingestion() -> None:
    document = _document()
    ingestor = _Ingestor()
    scheduler = _Scheduler()
    executor = ToolExecutor(
        _Retriever([[document]], []),
        site_page_ingestor=ingestor,
        completeness_policy=_policy(),
        completeness_facts=_Facts(_facts(fresh_count=0, soft_stale_count=2), _facts()),
        refresh_scheduler=CompletenessRefreshScheduler(scheduler),
    )

    result = executor.execute("search_documents", _document_arguments())

    assert result.evidence == [document]
    assert result.warning is not None
    assert len(scheduler.calls) == 1
    assert ingestor.calls == []


def test_refresh_scheduler_rejects_over_limit_target_set_without_partial_claim() -> (
    None
):
    scheduler = _Scheduler()

    result = CompletenessRefreshScheduler(scheduler, max_targets=1).schedule(
        urls=(
            "https://www.nptu.edu.tw/p4-a",
            "https://www.nptu.edu.tw/p4-b",
        ),
        unit=None,
        reason="stale_but_usable",
    )

    assert result.attempted is True
    assert result.succeeded is False
    assert result.scheduled_count == 0
    assert result.reason == "refresh_target_limit_exceeded"
    assert scheduler.calls == []


def test_refresh_scheduler_durably_marks_source_only_announcement_targets() -> None:
    scheduler = _Scheduler()

    result = CompletenessRefreshScheduler(scheduler).schedule(
        urls=(),
        source_names=("official-announcements",),
        unit=None,
        reason="stale_but_usable",
    )

    assert result.succeeded is True
    assert result.target_count == 1
    assert result.scheduled_count == 1
    assert scheduler.calls == [
        {
            "source": {
                "source_names": ("official-announcements",),
                "deadline": None,
            }
        }
    ]


def test_refresh_scheduler_rejects_partial_source_durable_schedule() -> None:
    class MissingSourceScheduler(_Scheduler):
        def schedule_announcement_sources(self, **kwargs: object) -> int:
            self.calls.append({"source": kwargs})
            return 0

    scheduler = MissingSourceScheduler()

    result = CompletenessRefreshScheduler(scheduler).schedule(
        urls=(),
        source_names=("missing-source",),
        unit=None,
        reason="insufficient_source_coverage",
    )

    assert result.succeeded is False
    assert result.scheduled_count == 0
    assert result.reason == "source_schedule_incomplete"


def test_refresh_scheduler_rejects_partial_page_durable_schedule() -> None:
    scheduler = _Scheduler()

    result = CompletenessRefreshScheduler(scheduler).schedule(
        urls=(
            "https://www.nptu.edu.tw/p4-a",
            "https://www.nptu.edu.tw/p4-b",
        ),
        unit=None,
        reason="stale_but_usable",
    )

    assert result.succeeded is False
    assert result.scheduled_count == 1
    assert result.reason == "page_schedule_incomplete"


def test_insufficient_document_uses_capped_live_fallback_then_rereads_db() -> None:
    document = _document()
    ingestor = _Ingestor()
    limits = SearchExecutionLimits(
        max_pages=4,
        max_candidate_urls=12,
        max_depth=1,
        max_pages_per_host=1,
    )
    executor = ToolExecutor(
        _Retriever([[], [document]], []),
        site_page_ingestor=ingestor,
        completeness_policy=_policy(),
        completeness_facts=_Facts(
            _facts(current_document_count=0, strong_evidence_count=0), _facts()
        ),
        live_fallback_limits=limits,
        live_fallback_max_seconds=8.0,
    )

    result = executor.execute("search_documents", _document_arguments())

    assert result.evidence == [document]
    assert len(ingestor.calls) == 1
    assert ingestor.calls[0]["limits"] == limits
    assert ingestor.calls[0]["deadline"].remaining_seconds() <= 8.0


def test_fresh_keyword_announcement_queries_db_before_live_keyword_ingestion() -> None:
    announcement = _announcement()
    executor = ToolExecutor(
        _Retriever([], [announcement]),
        refresher=_MustNotRun(),
        keyword_ingestor=_MustNotRun(),
        completeness_policy=_policy(),
        completeness_facts=_Facts(_facts(), _facts()),
    )

    result = executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": "獎學金",
                "limit": 5,
                "sort": AnnouncementSort.RELEVANCE.value,
                "unit": None,
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        ),
    )

    assert result.evidence == [announcement]


def test_insufficient_keyword_announcement_uses_bounded_shared_deadline() -> None:
    ingestor = _Ingestor()
    keyword = _KeywordIngestor()
    limits = SearchExecutionLimits(4, 12, 1, 1)
    executor = ToolExecutor(
        _Retriever([], []),
        keyword_ingestor=keyword,
        site_page_ingestor=ingestor,
        completeness_policy=_policy(),
        completeness_facts=_Facts(
            _facts(), _facts(current_document_count=0, strong_evidence_count=0)
        ),
        live_fallback_limits=limits,
        live_fallback_max_seconds=8.0,
    )

    executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": "獎學金",
                "limit": 5,
                "sort": AnnouncementSort.RELEVANCE.value,
                "unit": None,
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        ),
    )

    assert len(keyword.calls) == 1
    assert keyword.calls[0]["limits"] == limits
    assert keyword.calls[0]["max_items"] == 4
    deadline = keyword.calls[0]["deadline"]
    assert isinstance(deadline, SearchDeadline)
    assert deadline.remaining_seconds() <= 8.0


def test_terminal_incomplete_announcement_uses_db_without_another_live_attempt() -> (
    None
):
    announcement = _announcement()
    executor = ToolExecutor(
        _Retriever([], [announcement]),
        keyword_ingestor=_MustNotRun(),
        site_page_ingestor=_Ingestor(),
        completeness_policy=_policy(),
        completeness_facts=_Facts(_facts(), _facts(incomplete_announcement_count=1)),
    )

    result = executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": "獎學金",
                "limit": 5,
                "sort": AnnouncementSort.RELEVANCE.value,
                "unit": None,
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        ),
    )

    assert result.evidence == [announcement]
    assert result.warning is not None


def test_active_document_refresh_returns_db_without_live_ingestion() -> None:
    document = _document()
    ingestor = _Ingestor()
    executor = ToolExecutor(
        _Retriever([[document]], []),
        site_page_ingestor=ingestor,
        completeness_policy=_policy(),
        completeness_facts=_Facts(_facts(active_refresh_count=1), _facts()),
    )

    result = executor.execute("search_documents", _document_arguments())

    assert result.evidence == [document]
    assert result.warning is not None
    assert ingestor.calls == []


def test_shadow_mode_records_policy_but_keeps_legacy_document_live_flow() -> None:
    document = _document()
    ingestor = _Ingestor()
    shadow_policy = DbFirstCompletenessPolicy(
        CompletenessConfig(rollout_mode=CompletenessMode.SHADOW)
    )
    executor = ToolExecutor(
        _Retriever([[], [document]], []),
        site_page_ingestor=ingestor,
        completeness_policy=shadow_policy,
        completeness_facts=_Facts(
            _facts(current_document_count=0, strong_evidence_count=0), _facts()
        ),
    )

    result = executor.execute("search_documents", _document_arguments())

    assert result.evidence == [document]
    assert len(ingestor.calls) == 1


def test_scoped_announcement_uses_caller_shared_deadline_in_shadow_mode() -> None:
    ingestor = _ScopedIngestor()
    shared_deadline = SearchDeadline.after(10.0)
    executor = ToolExecutor(
        _Retriever([], []),
        site_page_ingestor=ingestor,
        unit_resolver=_scoped_unit_resolver(),
        completeness_policy=DbFirstCompletenessPolicy(
            CompletenessConfig(rollout_mode=CompletenessMode.SHADOW)
        ),
        completeness_facts=_Facts(
            _facts(current_document_count=0, strong_evidence_count=0),
            _facts(current_document_count=0, strong_evidence_count=0),
        ),
    )

    executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": None,
                "limit": 2,
                "sort": AnnouncementSort.NEWEST.value,
                "unit": "管理學院",
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        ),
        deadline=shared_deadline,
    )

    assert len(ingestor.calls) == 1
    assert ingestor.calls[0]["deadline"] is shared_deadline


def test_off_mode_does_not_request_completeness_facts() -> None:
    document = _document()
    ingestor = _Ingestor()

    class _FactsMustNotRun:
        def document_facts(
            self, *_args: object, **_kwargs: object
        ) -> CompletenessFacts:
            raise AssertionError("off mode 不得查 completeness facts")

        def announcement_facts(
            self, *_args: object, **_kwargs: object
        ) -> CompletenessFacts:
            raise AssertionError("off mode 不得查 completeness facts")

    executor = ToolExecutor(
        _Retriever([[], [document]], []),
        site_page_ingestor=ingestor,
        completeness_policy=DbFirstCompletenessPolicy(
            CompletenessConfig(rollout_mode=CompletenessMode.OFF)
        ),
        completeness_facts=_FactsMustNotRun(),
    )

    result = executor.execute("search_documents", _document_arguments())

    assert result.evidence == [document]
    assert len(ingestor.calls) == 1
