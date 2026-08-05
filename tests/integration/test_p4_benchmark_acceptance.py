from __future__ import annotations

import hashlib
import json
import os
import statistics
import time
from collections import Counter
from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from nptu_assistant.api.schemas import AnswerType, IngestionSummary
from nptu_assistant.crawlers.crawl_scheduler import CrawlScheduler
from nptu_assistant.crawlers.site_map import SiteCrawlStatus, SitePageType
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDiagnostics,
    SearchExecutionLimits,
    SearchPlan,
)
from nptu_assistant.crawlers.site_search import SitePageIngestionResult
from nptu_assistant.db.crawl_scheduler import SqlCrawlSchedulerRepository
from nptu_assistant.db.models import Document, SitePage, Source
from nptu_assistant.rag.completeness import (
    CompletenessConfig,
    CompletenessMode,
    DbFirstCompletenessPolicy,
)
from nptu_assistant.rag.completeness_facts import SqlRetrievalCompletenessFacts
from nptu_assistant.rag.completeness_refresh import (
    CompletenessRefreshScheduler,
    RefreshScheduleResult,
)
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.tools import AnnouncementSort, ToolExecutor
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.orm import Session, sessionmaker


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires a migrated PostgreSQL database with pgvector",
)

_FRESH_QUERY_COUNT = 60
_STALE_QUERY_COUNT = 25
_INSUFFICIENT_QUERY_COUNT = 15
_TOTAL_QUERY_COUNT = _FRESH_QUERY_COUNT + _STALE_QUERY_COUNT + _INSUFFICIENT_QUERY_COUNT


@dataclass(frozen=True, slots=True)
class _WorkloadMetrics:
    mode: str
    requests: int
    live_fallbacks: int
    external_http_calls: int
    live_ingestion_embedding_calls: int
    query_embedding_calls: int
    retrieval_calls: int
    total_sql_statements: int
    metadata_data_statements: int
    durable_refresh_schedule_calls: int
    durable_refresh_target_count: int
    durable_refresh_scheduled_count: int
    request_p50_ms: float
    request_p95_ms: float


class _BenchmarkRetriever:
    """Deterministic retrieval boundary with request-scoped embedding reuse.

    PostgreSQL completeness facts and scheduling remain production components.
    This retriever only removes ranking variance from the acceptance workload.
    """

    def __init__(self, evidence_by_query: dict[str, list[Evidence]]) -> None:
        self._evidence_by_query = evidence_by_query
        self.calls: Counter[str] = Counter()
        self.query_embedding_calls = 0
        self._execution_contexts: list[object] = []

    def search_documents_with_plan(
        self,
        *,
        plan: SearchPlan,
        limit: int,
        deadline: SearchDeadline | None = None,
        scope: object | None = None,
        execution_context: object | None = None,
    ) -> list[Evidence]:
        del deadline, scope
        self.calls[plan.query] += 1
        if execution_context is None or not any(
            item is execution_context for item in self._execution_contexts
        ):
            self.query_embedding_calls += 1
            if execution_context is not None:
                self._execution_contexts.append(execution_context)
        return list(self._evidence_by_query.get(plan.query, ()))[:limit]

    def search_documents(self, *, query: str, limit: int) -> list[Evidence]:
        return list(self._evidence_by_query.get(query, ()))[:limit]

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
    ) -> list[Evidence]:
        del query, limit, sort, unit, date_from, date_to, canonical_urls
        return []

    def get_announcement(self, announcement_id: str) -> Evidence | None:
        del announcement_id
        return None


class _CountingRefreshScheduler:
    """Measure actual production durable scheduler results for the workload."""

    def __init__(self, delegate: CompletenessRefreshScheduler) -> None:
        self._delegate = delegate
        self.calls = 0
        self.target_count = 0
        self.scheduled_count = 0

    def schedule(
        self,
        *,
        urls: tuple[str, ...],
        source_names: tuple[str, ...] = (),
        unit: str | None,
        reason: str,
        deadline: SearchDeadline | None = None,
    ) -> RefreshScheduleResult:
        result = self._delegate.schedule(
            urls=urls,
            source_names=source_names,
            unit=unit,
            reason=reason,
            deadline=deadline,
        )
        self.calls += 1
        self.target_count += result.target_count
        self.scheduled_count += result.scheduled_count
        return result


class _InstrumentedLiveProbe:
    """Deterministic substitute for public HTTP used only by this benchmark."""

    def __init__(self) -> None:
        self.live_queries: list[str] = []
        self.external_http_calls = 0
        self.live_ingestion_embedding_calls = 0
        self.received_limits: list[SearchExecutionLimits | None] = []
        self.received_deadlines: list[SearchDeadline] = []

    def new_deadline(self) -> SearchDeadline:
        return SearchDeadline.after(15.0)

    def should_search_live(self, evidence: Collection[object]) -> bool:
        del evidence
        return True

    def ingest(
        self,
        plan: SearchPlan,
        *,
        max_items: int,
        deadline: SearchDeadline,
        scope: object | None = None,
        limits: SearchExecutionLimits | None = None,
        execution_context: object | None = None,
    ) -> SitePageIngestionResult:
        del max_items, scope, execution_context
        deadline.raise_if_expired()
        self.live_queries.append(plan.query)
        self.received_limits.append(limits)
        self.received_deadlines.append(deadline)
        self.external_http_calls += 1
        self.live_ingestion_embedding_calls += 1
        return SitePageIngestionResult(
            IngestionSummary(),
            None,
            SearchDiagnostics(),
            relevant_pages_found=0,
            relevant_pages_persisted=0,
        )


def _factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _query(kind: str, index: int) -> str:
    return f"P4.0.1 benchmark {kind} query {index:03d} official procedure"


def _arguments(query: str) -> str:
    return json.dumps(
        {
            "query": query,
            "search_queries": [query],
            "concepts": ["P4.0.1", "official procedure"],
            "limit": 2,
        },
        ensure_ascii=False,
    )


def _seed_workload(
    factory: sessionmaker[Session],
    *,
    token: str,
    now: datetime,
) -> tuple[str, dict[str, list[Evidence]], tuple[str, ...]]:
    host = f"p4-benchmark-{token}.nptu.edu.tw"
    prefix = f"https://{host}"
    unit = f"P4.0.1 benchmark {token}"
    source_id = uuid4()
    source_row = {
        "id": source_id,
        "name": f"p4-0-1-benchmark-{token}",
        "base_url": prefix,
        "unit": unit,
        "source_type": "document",
        "crawl_enabled": True,
        "crawl_interval_minutes": 60,
        "canonical_urls": [],
        "last_successful_crawl_at": now,
    }
    evidence_by_query: dict[str, list[Evidence]] = {}
    page_rows: list[dict[str, object]] = []
    document_rows: list[dict[str, object]] = []
    stale_urls: list[str] = []

    for kind, count, age in (
        ("fresh", _FRESH_QUERY_COUNT, timedelta()),
        ("stale", _STALE_QUERY_COUNT, timedelta(hours=7)),
    ):
        for index in range(count):
            query = _query(kind, index)
            evidence: list[Evidence] = []
            for rank in range(2):
                path = f"/{kind}/{index:03d}/{rank}"
                url = f"{prefix}{path}"
                digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
                content = (
                    f"{query} is documented by this official benchmark page. "
                    "The page contains eligibility, steps, deadlines, and required "
                    "documents for deterministic DB-first completeness acceptance. "
                ) * 3
                page_rows.append(
                    {
                        "id": uuid4(),
                        "canonical_url": url,
                        "host": host,
                        "path": path,
                        "title": f"P4.0.1 {kind} benchmark {index:03d}-{rank}",
                        "unit": unit,
                        "page_type": SitePageType.GENERAL_PAGE.value,
                        "discovery_source": "manual",
                        "crawl_status": SiteCrawlStatus.SUCCESS.value,
                        "next_crawl_at": now + timedelta(hours=4),
                        "last_successful_crawl_at": now - age,
                        "content_hash": digest,
                        "ingestion_content_hash": digest,
                        "ingestion_status": "success",
                        "announcement_ingestion_status": "not_applicable",
                        "is_active": True,
                        "is_indexable": True,
                        "crawl_priority": 1,
                        "failure_count": 0,
                        "minimum_depth": 0,
                    }
                )
                document_rows.append(
                    {
                        "id": uuid4(),
                        "source_id": source_id,
                        "title": f"P4.0.1 {kind} benchmark {index:03d}-{rank}",
                        "canonical_url": url,
                        "document_type": "official_web_page",
                        "published_at": date(2026, 8, 5),
                        "version": digest[:12],
                        "content_hash": digest,
                        "raw_text": content,
                        "is_current": True,
                    }
                )
                evidence.append(
                    Evidence(
                        id=f"{kind}-{index:03d}-{rank}",
                        kind=AnswerType.OFFICIAL_DOCUMENT,
                        title=f"P4.0.1 {kind} benchmark {index:03d}-{rank}",
                        url=url,
                        unit=unit,
                        published_at=date(2026, 8, 5),
                        content=content,
                        score=0.96 if rank == 0 else 0.90,
                    )
                )
                if kind == "stale":
                    stale_urls.append(url)
            evidence_by_query[query] = evidence

    for index in range(_INSUFFICIENT_QUERY_COUNT):
        evidence_by_query[_query("insufficient", index)] = []

    with factory.begin() as session:
        session.execute(Source.__table__.insert(), [source_row])
        session.execute(SitePage.__table__.insert(), page_rows)
        session.execute(Document.__table__.insert(), document_rows)

    return prefix, evidence_by_query, tuple(stale_urls)


def _cleanup(factory: sessionmaker[Session], *, prefix: str, token: str) -> None:
    with factory.begin() as session:
        session.execute(
            delete(Document).where(Document.canonical_url.like(f"{prefix}%"))
        )
        session.execute(
            delete(SitePage).where(SitePage.canonical_url.like(f"{prefix}%"))
        )
        session.execute(
            delete(Source).where(Source.name == f"p4-0-1-benchmark-{token}")
        )


def _run_workload(
    factory: sessionmaker[Session],
    engine: object,
    *,
    evidence_by_query: dict[str, list[Evidence]],
    mode: CompletenessMode,
    now: datetime,
    limits: SearchExecutionLimits,
) -> tuple[_WorkloadMetrics, _InstrumentedLiveProbe]:
    retriever = _BenchmarkRetriever(evidence_by_query)
    live_probe = _InstrumentedLiveProbe()
    policy = DbFirstCompletenessPolicy(CompletenessConfig(rollout_mode=mode))
    refresh_scheduler = _CountingRefreshScheduler(
        CompletenessRefreshScheduler(
            CrawlScheduler(SqlCrawlSchedulerRepository(factory))
        )
    )
    executor = ToolExecutor(
        retriever,
        site_page_ingestor=live_probe,
        completeness_policy=policy,
        completeness_facts=SqlRetrievalCompletenessFacts(factory),
        refresh_scheduler=refresh_scheduler,
        live_fallback_limits=limits,
        live_fallback_max_seconds=8.0,
        now=lambda: now,
    )
    statements: list[str] = []

    def capture(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.strip():
            statements.append(statement.strip())

    event.listen(engine, "before_cursor_execute", capture)
    latencies_ms: list[float] = []
    try:
        for kind, count in (
            ("fresh", _FRESH_QUERY_COUNT),
            ("stale", _STALE_QUERY_COUNT),
            ("insufficient", _INSUFFICIENT_QUERY_COUNT),
        ):
            for index in range(count):
                query = _query(kind, index)
                started = time.perf_counter()
                result = executor.execute("search_documents", _arguments(query))
                latencies_ms.append((time.perf_counter() - started) * 1_000)
                if kind == "fresh":
                    assert len(result.evidence) == 2
                    assert result.warning is None
                elif kind == "stale":
                    assert len(result.evidence) == 2
                    if mode is CompletenessMode.ENFORCE:
                        assert result.warning is not None
                else:
                    assert result.evidence == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    data_statements = [
        statement
        for statement in statements
        if not statement.upper().startswith("SET LOCAL STATEMENT_TIMEOUT")
    ]
    metrics = _WorkloadMetrics(
        mode=mode.value,
        requests=_TOTAL_QUERY_COUNT,
        live_fallbacks=len(live_probe.live_queries),
        external_http_calls=live_probe.external_http_calls,
        live_ingestion_embedding_calls=live_probe.live_ingestion_embedding_calls,
        query_embedding_calls=retriever.query_embedding_calls,
        retrieval_calls=sum(retriever.calls.values()),
        total_sql_statements=len(statements),
        metadata_data_statements=len(data_statements),
        durable_refresh_schedule_calls=refresh_scheduler.calls,
        durable_refresh_target_count=refresh_scheduler.target_count,
        durable_refresh_scheduled_count=refresh_scheduler.scheduled_count,
        request_p50_ms=round(statistics.median(latencies_ms), 3),
        request_p95_ms=round(
            statistics.quantiles(latencies_ms, n=100, method="inclusive")[94],
            3,
        ),
    )
    return metrics, live_probe


def test_p4_0_1_off_vs_enforce_tool_executor_benchmark() -> None:
    """Compare legacy and P4 on the same 100-request ToolExecutor workload.

    The live probe deliberately replaces public HTTP, while policy, facts,
    deadlines, durable scheduling, and PostgreSQL state use production code.
    """

    factory, engine = _factory()
    token = uuid4().hex
    now = datetime.now(timezone.utc)
    limits = SearchExecutionLimits(
        max_pages=6,
        max_candidate_urls=16,
        max_depth=1,
        max_pages_per_host=6,
    )
    prefix, evidence_by_query, stale_urls = _seed_workload(
        factory,
        token=token,
        now=now,
    )
    insufficient_queries = {
        _query("insufficient", index) for index in range(_INSUFFICIENT_QUERY_COUNT)
    }
    try:
        legacy, legacy_probe = _run_workload(
            factory,
            engine,
            evidence_by_query=evidence_by_query,
            mode=CompletenessMode.OFF,
            now=now,
            limits=limits,
        )
        p4, p4_probe = _run_workload(
            factory,
            engine,
            evidence_by_query=evidence_by_query,
            mode=CompletenessMode.ENFORCE,
            now=now,
            limits=limits,
        )

        fallback_reduction = (
            legacy.live_fallbacks - p4.live_fallbacks
        ) / legacy.live_fallbacks

        assert legacy.live_fallbacks == _TOTAL_QUERY_COUNT
        assert p4.live_fallbacks == _INSUFFICIENT_QUERY_COUNT
        assert fallback_reduction >= 0.70
        assert set(p4_probe.live_queries) == insufficient_queries
        assert legacy.external_http_calls == _TOTAL_QUERY_COUNT
        assert p4.external_http_calls == _INSUFFICIENT_QUERY_COUNT
        assert legacy.live_ingestion_embedding_calls == _TOTAL_QUERY_COUNT
        assert p4.live_ingestion_embedding_calls == _INSUFFICIENT_QUERY_COUNT
        assert legacy.query_embedding_calls == _TOTAL_QUERY_COUNT
        assert p4.query_embedding_calls == _TOTAL_QUERY_COUNT
        assert legacy.retrieval_calls == _TOTAL_QUERY_COUNT * 2
        assert p4.retrieval_calls == (
            _FRESH_QUERY_COUNT + _STALE_QUERY_COUNT + _INSUFFICIENT_QUERY_COUNT * 2
        )
        assert all(item is None for item in legacy_probe.received_limits)
        assert p4_probe.received_limits == [limits] * _INSUFFICIENT_QUERY_COUNT
        assert all(
            0.0 < deadline.remaining_seconds() <= 8.0
            for deadline in p4_probe.received_deadlines
        )
        assert legacy.durable_refresh_schedule_calls == 0
        assert legacy.durable_refresh_target_count == 0
        assert legacy.durable_refresh_scheduled_count == 0
        assert p4.durable_refresh_schedule_calls == _STALE_QUERY_COUNT
        assert p4.durable_refresh_target_count == _STALE_QUERY_COUNT * 2
        assert p4.durable_refresh_scheduled_count == _STALE_QUERY_COUNT * 2
        assert p4.metadata_data_statements <= 125
        assert p4.request_p95_ms < 250.0

        with factory() as session:
            scheduled_stale_pages = session.scalar(
                select(func.count())
                .select_from(SitePage)
                .where(
                    SitePage.canonical_url.in_(stale_urls),
                    SitePage.next_crawl_at <= datetime.now(timezone.utc),
                )
            )
        assert scheduled_stale_pages == len(stale_urls)

        print(
            json.dumps(
                {
                    "legacy": asdict(legacy),
                    "p4": asdict(p4),
                    "fallback_reduction_percent": round(
                        fallback_reduction * 100,
                        1,
                    ),
                    "fresh_requests_without_live": _FRESH_QUERY_COUNT,
                    "stale_requests_scheduled_without_live": _STALE_QUERY_COUNT,
                    "insufficient_requests_with_bounded_live": (
                        _INSUFFICIENT_QUERY_COUNT
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        _cleanup(factory, prefix=prefix, token=token)
        engine.dispose()
