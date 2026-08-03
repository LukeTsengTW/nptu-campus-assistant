from __future__ import annotations

import json
import os
import statistics
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from nptu_assistant.crawlers.crawl_scheduler import CrawlScheduler
from nptu_assistant.crawlers.config import CrawlerSourceConfig, SiteSearchConfig
from nptu_assistant.crawlers.resolution import UnitSourceResolver
from nptu_assistant.crawlers.site_map import SiteCrawlStatus, SitePageType
from nptu_assistant.crawlers.site_models import SearchDeadline, SearchExecutionLimits
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    SitePageIngestionService,
)
from nptu_assistant.db.crawl_models import SiteCrawlAttempt
from nptu_assistant.db.crawl_scheduler import SqlCrawlSchedulerRepository
from nptu_assistant.db.models import (
    Announcement,
    Document,
    DocumentChunk,
    SitePage,
    Source,
)
from nptu_assistant.db.repositories import SqlDocumentRepository
from nptu_assistant.ingestion.chunking import chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.rag.completeness import (
    CompletenessAction,
    CompletenessConfig,
    CompletenessMode,
    DbFirstCompletenessPolicy,
    QueryIntent,
)
from nptu_assistant.rag.completeness_facts import (
    SqlRetrievalCompletenessFacts,
    document_fact_rows_statement,
)
from nptu_assistant.rag.completeness_refresh import CompletenessRefreshScheduler
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.tools import ToolExecutor
from nptu_assistant.api.schemas import AnswerType
from sqlalchemy import create_engine, delete, event, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires a migrated PostgreSQL database with pgvector",
)


class _NoLiveIngestor:
    def __init__(self) -> None:
        self.calls = 0

    def new_deadline(self) -> SearchDeadline:
        return SearchDeadline.after(15)

    def should_search_live(self, _evidence: object) -> bool:
        raise AssertionError("P4 complete/stale DB request must not reach live gate")

    def ingest(self, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("P4 complete/stale DB request must not ingest")


class _P4EmbeddingProvider:
    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        return [[1.0, *([0.0] * 1535)] for _text in texts]


class _FixtureSiteHttpClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        allowed_hosts: object | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        del allowed_hosts, timeout_seconds
        if deadline is not None:
            deadline.raise_if_expired()
        self.calls.append(url)
        return self._pages[url]


def _factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _seed_document(
    factory: sessionmaker[Session],
    *,
    token: str,
    last_successful_crawl_at: datetime,
    next_crawl_at: datetime,
) -> tuple[str, str]:
    unit = f"P4 整合單位 {token}"
    url = f"https://www.nptu.edu.tw/p4-completeness-{token}"
    raw_text = "P4 資料庫優先驗收文件，包含就學貸款申請流程、申請期限與官方說明。" * 12
    digest = content_hash(raw_text)
    metadata = DocumentMetadata(
        title=f"P4 就學貸款申請 {token}",
        source_url=url,
        unit=unit,
        published_at=date(2026, 8, 3),
        effective_from=date(2026, 8, 3),
        document_type="official_web_page",
        version=digest[:12],
    )
    chunks = chunk_text(raw_text)
    SqlDocumentRepository(factory).save_idempotent(
        metadata,
        raw_text,
        chunks,
        [[1.0, *([0.0] * 1535)] for _chunk in chunks],
    )
    with factory.begin() as session:
        session.add(
            SitePage(
                canonical_url=url,
                host="www.nptu.edu.tw",
                path=f"/p4-completeness-{token}",
                title=metadata.title,
                unit=unit,
                page_type=SitePageType.GENERAL_PAGE.value,
                crawl_status=SiteCrawlStatus.SUCCESS.value,
                next_crawl_at=next_crawl_at,
                last_successful_crawl_at=last_successful_crawl_at,
                content_hash=digest,
                ingestion_content_hash=digest,
                ingestion_status="success",
                is_active=True,
                is_indexable=True,
                crawl_priority=1,
            )
        )
    return url, unit


def _cleanup(factory: sessionmaker[Session], token: str, unit: str) -> None:
    url_match = f"%{token}%"
    with factory.begin() as session:
        page_ids = select(SitePage.id).where(SitePage.canonical_url.like(url_match))
        document_ids = select(Document.id).where(Document.canonical_url.like(url_match))
        session.execute(
            delete(SiteCrawlAttempt).where(SiteCrawlAttempt.site_page_id.in_(page_ids))
        )
        session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id.in_(document_ids))
        )
        session.execute(delete(Document).where(Document.canonical_url.like(url_match)))
        session.execute(delete(SitePage).where(SitePage.canonical_url.like(url_match)))
        session.execute(delete(Source).where(Source.name == f"document:{unit}"))


def _seed_announcement(
    factory: sessionmaker[Session],
    *,
    token: str,
    last_successful_crawl_at: datetime,
) -> tuple[str, str, str]:
    unit = f"P4 公告整合單位 {token}"
    source_name = f"p4-completeness-source-{token}"
    url = f"https://www.nptu.edu.tw/p4-completeness-announcement-{token}"
    digest = "c" * 64
    with factory.begin() as session:
        source = Source(
            name=source_name,
            base_url=f"https://www.nptu.edu.tw/p4-completeness-listing-{token}",
            unit=unit,
            source_type="announcement",
            crawl_enabled=True,
            crawl_interval_minutes=60,
            canonical_urls=[url],
            last_successful_crawl_at=last_successful_crawl_at,
        )
        session.add(source)
        session.flush()
        session.add(
            Announcement(
                source_id=source.id,
                title=f"P4 獎學金申請公告 {token}",
                unit=unit,
                category="整合測試",
                published_at=date(2026, 8, 3),
                canonical_url=url,
                body="P4 獎學金申請的日期、資格與送件流程均以此官方公告為準。" * 3,
                content_hash=digest,
                last_crawled_at=last_successful_crawl_at,
            )
        )
        session.add(
            SitePage(
                canonical_url=url,
                host="www.nptu.edu.tw",
                path=f"/p4-completeness-announcement-{token}",
                title=f"P4 獎學金申請公告 {token}",
                unit=unit,
                page_type=SitePageType.ANNOUNCEMENT_DETAIL.value,
                crawl_status=SiteCrawlStatus.SUCCESS.value,
                next_crawl_at=last_successful_crawl_at + timedelta(hours=1),
                last_successful_crawl_at=last_successful_crawl_at,
                content_hash=digest,
                ingestion_content_hash=digest,
                ingestion_status="success",
                announcement_ingestion_status="success",
                is_active=True,
                is_indexable=True,
                crawl_priority=1,
            )
        )
    return url, unit, source_name


def _cleanup_announcement(
    factory: sessionmaker[Session],
    *,
    url: str,
    source_name: str,
) -> None:
    with factory.begin() as session:
        session.execute(delete(Announcement).where(Announcement.canonical_url == url))
        session.execute(delete(SitePage).where(SitePage.canonical_url == url))
        session.execute(delete(Source).where(Source.name == source_name))


def _executor(
    factory: sessionmaker[Session],
    ingestor: _NoLiveIngestor,
    *,
    unit_resolver: UnitSourceResolver | None = None,
) -> ToolExecutor:
    policy = DbFirstCompletenessPolicy(
        CompletenessConfig(
            rollout_mode=CompletenessMode.ENFORCE,
            min_strong_evidence=1,
            exact_scope_min_score=0.0,
            minimum_score_margin=0.0,
        )
    )
    return ToolExecutor(
        SqlRetriever(factory, _P4EmbeddingProvider()),
        site_page_ingestor=ingestor,
        unit_resolver=unit_resolver,
        completeness_policy=policy,
        completeness_facts=SqlRetrievalCompletenessFacts(factory),
        refresh_scheduler=CompletenessRefreshScheduler(
            CrawlScheduler(SqlCrawlSchedulerRepository(factory))
        ),
    )


def _arguments() -> str:
    return json.dumps(
        {
            "query": "就學貸款申請流程",
            "search_queries": ["就學貸款申請"],
            "concepts": ["就學貸款", "申請流程"],
            "limit": 1,
        },
        ensure_ascii=False,
    )


def test_postgres_p4_fresh_warm_db_uses_real_retriever_without_live_ingestion() -> None:
    factory, engine = _factory()
    token = uuid4().hex
    now = datetime.now(timezone.utc)
    url, unit = _seed_document(
        factory,
        token=token,
        last_successful_crawl_at=now,
        next_crawl_at=now + timedelta(hours=4),
    )
    ingestor = _NoLiveIngestor()
    try:
        result = _executor(factory, ingestor).execute("search_documents", _arguments())

        assert result.warning is None
        assert [item.url for item in result.evidence] == [url]
        assert ingestor.calls == 0
        with factory() as session:
            assert session.scalar(
                select(Document.is_current).where(Document.canonical_url == url)
            )
    finally:
        _cleanup(factory, token, unit)
        engine.dispose()


def _plan_nodes(plan: dict[str, object]) -> set[str]:
    nodes = (
        {str(plan["Node Type"])} if isinstance(plan.get("Node Type"), str) else set()
    )
    children = plan.get("Plans")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                nodes.update(_plan_nodes(child))
    return nodes


def _plan_indexes(plan: dict[str, object]) -> set[str]:
    indexes = (
        {str(plan["Index Name"])} if isinstance(plan.get("Index Name"), str) else set()
    )
    children = plan.get("Plans")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                indexes.update(_plan_indexes(child))
    return indexes


def test_postgres_p4_soft_stale_db_schedules_without_live_ingestion() -> None:
    factory, engine = _factory()
    token = uuid4().hex
    before = datetime.now(timezone.utc)
    url, unit = _seed_document(
        factory,
        token=token,
        last_successful_crawl_at=before - timedelta(hours=7),
        next_crawl_at=before + timedelta(hours=4),
    )
    ingestor = _NoLiveIngestor()
    try:
        result = _executor(factory, ingestor).execute("search_documents", _arguments())

        assert result.warning is not None
        assert [item.url for item in result.evidence] == [url]
        assert ingestor.calls == 0
        with factory() as session:
            page = session.scalar(select(SitePage).where(SitePage.canonical_url == url))
            assert page is not None
            assert page.next_crawl_at is not None
            assert page.next_crawl_at <= datetime.now(timezone.utc)
    finally:
        _cleanup(factory, token, unit)
        engine.dispose()


def test_postgres_p4_active_lease_uses_db_without_competing_live_ingestion() -> None:
    factory, engine = _factory()
    token = uuid4().hex
    now = datetime.now(timezone.utc)
    url, unit = _seed_document(
        factory,
        token=token,
        last_successful_crawl_at=now,
        next_crawl_at=now + timedelta(hours=4),
    )
    ingestor = _NoLiveIngestor()
    try:
        with factory.begin() as session:
            session.execute(
                update(SitePage)
                .where(SitePage.canonical_url == url)
                .values(
                    crawl_lease_owner="p4-background-worker",
                    crawl_lease_token=uuid4(),
                    crawl_lease_expires_at=now + timedelta(minutes=5),
                )
            )

        result = _executor(factory, ingestor).execute("search_documents", _arguments())

        assert [item.url for item in result.evidence] == [url]
        assert result.warning is not None
        assert ingestor.calls == 0
    finally:
        _cleanup(factory, token, unit)
        engine.dispose()


def test_postgres_p4_fresh_scoped_announcement_uses_db_without_live_ingestion() -> None:
    factory, engine = _factory()
    token = uuid4().hex
    now = datetime.now(timezone.utc)
    url, unit, source_name = _seed_announcement(
        factory,
        token=token,
        last_successful_crawl_at=now,
    )
    ingestor = _NoLiveIngestor()
    resolver = UnitSourceResolver(
        [
            CrawlerSourceConfig(
                name=source_name,
                adapter="fixture",
                url="data/fixtures/p4.xml",
                unit=unit,
                aliases=[unit],
            )
        ],
        {},
    )
    try:
        result = _executor(
            factory,
            ingestor,
            unit_resolver=resolver,
        ).execute(
            "search_announcements",
            json.dumps(
                {
                    "query": "獎學金",
                    "limit": 1,
                    "sort": "relevance",
                    "unit": unit,
                    "date_from": None,
                    "date_to": None,
                },
                ensure_ascii=False,
            ),
        )

        assert [item.url for item in result.evidence] == [url]
        assert result.warning is None
        assert ingestor.calls == 0
    finally:
        _cleanup_announcement(factory, url=url, source_name=source_name)
        engine.dispose()


def test_postgres_p4_insufficient_document_uses_bounded_fixture_live_fallback() -> None:
    factory, engine = _factory()
    token = uuid4().hex
    unit = f"P4 bounded fallback {token}"
    root_url = f"https://www.nptu.edu.tw/p4-completeness-live-{token}/"
    target_url = f"https://www.nptu.edu.tw/p4-completeness-live-{token}/target"
    query = f"P4 bounded fallback {token} 校務申請流程"
    http = _FixtureSiteHttpClient(
        {
            root_url: (
                f'<main><h1>P4 首頁</h1><a href="{target_url}">官方詳細流程</a></main>'
            ),
            target_url: (
                f"<main><h1>{query}</h1><p>{query} 的官方流程、期限與應備文件說明。"
                "此頁為 PostgreSQL P4 fixture，不會連線公網。</p></main>"
            ),
        }
    )
    site_config = SiteSearchConfig(
        enabled=True,
        seed_urls=[root_url],
        allowed_hosts=["nptu.edu.tw"],
        max_pages=6,
        max_items=4,
        max_candidate_urls=16,
        max_depth=1,
        max_pages_per_host=6,
        relevance_threshold=0.0,
        early_stop_min_results=1,
        unit=unit,
        category="P4 fixture",
    )
    embedding = _P4EmbeddingProvider()
    document_repository = SqlDocumentRepository(factory)
    live_ingestor = SitePageIngestionService(
        NptuSiteSearchService(site_config, http),
        document_repository,
        embedding,
        site_config,
    )
    policy = DbFirstCompletenessPolicy(
        CompletenessConfig(
            rollout_mode=CompletenessMode.ENFORCE,
            min_strong_evidence=1,
            exact_scope_min_score=0.0,
            minimum_score_margin=0.0,
        )
    )
    limits = SearchExecutionLimits(
        max_pages=4,
        max_candidate_urls=12,
        max_depth=1,
        # The fixture has one root and one detail page on the same host.  It
        # remains bounded but must permit the linked target to be fetched.
        max_pages_per_host=2,
    )
    executor = ToolExecutor(
        SqlRetriever(factory, embedding),
        site_page_ingestor=live_ingestor,
        completeness_policy=policy,
        completeness_facts=SqlRetrievalCompletenessFacts(factory),
        refresh_scheduler=CompletenessRefreshScheduler(
            CrawlScheduler(SqlCrawlSchedulerRepository(factory))
        ),
        live_fallback_limits=limits,
        live_fallback_max_seconds=8.0,
    )
    try:
        result = executor.execute(
            "search_documents",
            json.dumps(
                {
                    "query": query,
                    "search_queries": [query],
                    "concepts": ["校務申請", "應備文件"],
                    "limit": 1,
                },
                ensure_ascii=False,
            ),
        )

        assert [item.url for item in result.evidence] == [target_url]
        assert 1 <= len(http.calls) <= limits.max_pages
        assert set(http.calls) <= {root_url, target_url}
        with factory() as session:
            current_count = session.scalar(
                select(Document.id)
                .where(
                    Document.canonical_url == target_url,
                    Document.is_current.is_(True),
                )
                .limit(1)
            )
        assert current_count is not None
    finally:
        _cleanup(factory, token, unit)
        engine.dispose()


def test_postgres_p4_completeness_metadata_workload_is_bounded() -> None:
    """Exercise 100 P4 decisions against a representative PostgreSQL fixture."""

    factory, engine = _factory()
    token = uuid4().hex
    prefix = f"https://p4-completeness-{token}.nptu.edu.tw"
    now = datetime.now(timezone.utc)
    document_source_id = uuid4()
    source_rows = [
        {
            "id": document_source_id,
            "name": f"p4-benchmark-document-{token}",
            "base_url": prefix,
            "unit": f"P4 benchmark {token}",
            "source_type": "document",
            "crawl_enabled": True,
            "crawl_interval_minutes": 60,
            "canonical_urls": [],
            "last_successful_crawl_at": now,
        }
    ]
    announcement_source_ids = [uuid4() for _ in range(20)]
    source_rows.extend(
        {
            "id": source_id,
            "name": f"p4-benchmark-announcement-{token}-{index}",
            "base_url": f"{prefix}/announcement-source-{index}",
            "unit": f"P4 benchmark unit {index % 10}",
            "source_type": "announcement",
            "crawl_enabled": True,
            "crawl_interval_minutes": 60,
            "canonical_urls": [
                f"{prefix}/page/{item}" for item in range(index, 2_000, 20)
            ],
            "last_successful_crawl_at": now,
        }
        for index, source_id in enumerate(announcement_source_ids)
    )
    page_rows: list[dict[str, object]] = []
    document_rows: list[dict[str, object]] = []
    superseded_rows: list[dict[str, object]] = []
    announcement_rows: list[dict[str, object]] = []
    for index in range(10_000):
        url = f"{prefix}/page/{index}"
        age = (
            timedelta(0)
            if index < 60
            else timedelta(hours=7)
            if index < 85
            else timedelta(days=2)
        )
        digest = f"{index:064x}"
        page_rows.append(
            {
                "id": uuid4(),
                "canonical_url": url,
                "host": f"p4-completeness-{index % 20}.nptu.edu.tw",
                "path": f"/page/{index}",
                "title": f"P4 completeness benchmark page {index}",
                "page_type": SitePageType.GENERAL_PAGE.value,
                "discovery_source": "manual",
                "crawl_status": SiteCrawlStatus.SUCCESS.value,
                "next_crawl_at": now + timedelta(hours=1),
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
        if index < 5_000:
            document_rows.append(
                {
                    "id": uuid4(),
                    "source_id": document_source_id,
                    "title": f"P4 completeness benchmark document {index}",
                    "canonical_url": url,
                    "document_type": "official_web_page",
                    "published_at": date(2026, 8, 3),
                    "version": digest[:12],
                    "content_hash": digest,
                    "raw_text": "P4 completeness benchmark official document content."
                    * 8,
                    "is_current": True,
                }
            )
        if index < 1_000:
            superseded_rows.append(
                {
                    "id": uuid4(),
                    "source_id": document_source_id,
                    "title": f"P4 benchmark superseded document {index}",
                    "canonical_url": url,
                    "document_type": "official_web_page",
                    "published_at": date(2026, 7, 1),
                    "version": f"old-{index}",
                    "content_hash": f"{10_000 + index:064x}",
                    "raw_text": "P4 historical superseded document." * 8,
                    "is_current": False,
                }
            )
        if index < 2_000:
            announcement_rows.append(
                {
                    "id": uuid4(),
                    "source_id": announcement_source_ids[index % 20],
                    "title": f"P4 benchmark announcement {index}",
                    "unit": f"P4 benchmark unit {index % 10}",
                    "published_at": date(2026, 8, 3),
                    "canonical_url": url,
                    "body": "P4 benchmark announcement official body." * 6,
                    "content_hash": f"{20_000 + index:064x}",
                    "last_crawled_at": now,
                }
            )
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.strip():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with factory.begin() as session:
            session.execute(Source.__table__.insert(), source_rows)
            session.execute(SitePage.__table__.insert(), page_rows)
            session.execute(Document.__table__.insert(), document_rows)
            session.execute(Document.__table__.insert(), superseded_rows)
            session.execute(Announcement.__table__.insert(), announcement_rows)
            session.execute(text("ANALYZE site_pages"))
            session.execute(text("ANALYZE documents"))
            session.execute(text("ANALYZE announcements"))

        facts_provider = SqlRetrievalCompletenessFacts(factory)
        policy = DbFirstCompletenessPolicy(
            CompletenessConfig(
                min_strong_evidence=1,
                exact_scope_min_score=0.0,
                minimum_score_margin=0.0,
            )
        )
        scheduler = CompletenessRefreshScheduler(
            CrawlScheduler(SqlCrawlSchedulerRepository(factory))
        )
        query_times_ms: list[float] = []
        actions: Counter[CompletenessAction] = Counter()
        statement_start = len(statements)
        for index in range(100):
            evidence = (
                []
                if index >= 85
                else [
                    Evidence(
                        id=f"p4-benchmark-{index}",
                        kind=AnswerType.OFFICIAL_DOCUMENT,
                        title=f"P4 benchmark {index}",
                        url=f"{prefix}/page/{index}",
                        unit=f"P4 benchmark {token}",
                        published_at=date(2026, 8, 3),
                        content="P4 benchmark evidence with sufficient official content."
                        * 8,
                        score=0.95,
                    )
                ]
            )
            started = time.perf_counter()
            facts = facts_provider.document_facts(
                evidence,
                scope=None,
                now=now,
                strong_score=0.0,
                min_content_chars=160,
                soft_stale=timedelta(hours=6),
                hard_stale=timedelta(hours=24),
            )
            decision = policy.decide(
                facts=facts,
                intent=QueryIntent.TOPIC,
                remaining_deadline_seconds=8.0,
            )
            if decision.action is CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH:
                result = scheduler.schedule(
                    urls=decision.schedule_targets,
                    unit=None,
                    reason=decision.reason_codes[0],
                )
                assert result.succeeded
            query_times_ms.append((time.perf_counter() - started) * 1_000)
            actions[decision.action] += 1

        statement_count = len(statements) - statement_start
        assert actions == Counter(
            {
                CompletenessAction.USE_DB: 60,
                CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH: 25,
                CompletenessAction.USE_BOUNDED_LIVE_FALLBACK: 15,
            }
        )
        assert statement_count <= 125
        p50_ms = statistics.median(query_times_ms)
        p95_ms = statistics.quantiles(query_times_ms, n=100, method="inclusive")[94]
        assert p95_ms < 250

        statement = document_fact_rows_statement((f"{prefix}/page/0",))
        compiled = statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
        with engine.connect() as connection:
            raw_plan = connection.execute(
                text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}")
            ).scalar_one()
        payload = raw_plan[0] if isinstance(raw_plan, list) else raw_plan
        assert isinstance(payload, dict)
        root = payload["Plan"]
        assert isinstance(root, dict)
        assert float(payload["Execution Time"]) < 250

        print(
            json.dumps(
                {
                    "dataset": {
                        "site_pages": 10_000,
                        "current_documents": 5_000,
                        "superseded_documents": 1_000,
                        "announcements": 2_000,
                        "hosts": 20,
                    },
                    "workload": {
                        "fresh_complete": 60,
                        "stale_usable": 25,
                        "insufficient": 15,
                        "external_http_calls": 0,
                        "ingestion_embedding_calls": 0,
                        "decision_counts": {
                            action.value: count for action, count in actions.items()
                        },
                        "statement_count": statement_count,
                        "policy_query_p50_ms": p50_ms,
                        "policy_query_p95_ms": p95_ms,
                    },
                    "explain": {
                        "planning_ms": float(payload["Planning Time"]),
                        "execution_ms": float(payload["Execution Time"]),
                        "returned_rows": int(root.get("Actual Rows", 0)),
                        "node_types": sorted(_plan_nodes(root)),
                        "indexes": sorted(_plan_indexes(root)),
                        "shared_hit_blocks": int(root.get("Shared Hit Blocks", 0) or 0),
                        "shared_read_blocks": int(
                            root.get("Shared Read Blocks", 0) or 0
                        ),
                    },
                },
                ensure_ascii=False,
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        with factory.begin() as session:
            session.execute(
                delete(Announcement).where(
                    Announcement.canonical_url.like(f"{prefix}%")
                )
            )
            session.execute(
                delete(Document).where(Document.canonical_url.like(f"{prefix}%"))
            )
            session.execute(
                delete(SitePage).where(SitePage.canonical_url.like(f"{prefix}%"))
            )
            session.execute(
                delete(Source).where(Source.name.like(f"p4-benchmark-%{token}%"))
            )
        engine.dispose()
