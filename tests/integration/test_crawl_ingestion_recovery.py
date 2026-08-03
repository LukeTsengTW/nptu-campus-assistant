from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuListingItem,
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.announcement_adapter import (
    AnnouncementSourceIdentity,
    IncrementalAnnouncementAdapter,
)
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.crawl_ingestion import (
    CrawlIngestionService,
    CrawlIngestionStatus,
)
from nptu_assistant.crawlers.crawl_scheduler import CrawlScheduler
from nptu_assistant.crawlers.http import CrawlHttpResponse
from nptu_assistant.crawlers.incremental_crawler import (
    IncrementalCrawler,
    IncrementalCrawlOutcome,
)
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.site_map import (
    SiteCrawlStatus,
    SiteMapService,
    SitePageType,
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
from nptu_assistant.db.repositories import (
    SqlAnnouncementRepository,
    SqlDocumentRepository,
)
from nptu_assistant.db.site_map import SqlSiteMapRepository
from nptu_assistant.ingestion.chunking import chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="需要已套用 migration 的 PostgreSQL/pgvector 資料庫",
)


class _MutableClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.value.timestamp()


class _OneBodyHttpClient:
    def __init__(self, url: str, body: str) -> None:
        self.url = url
        self.body = body.encode("utf-8")
        self.requests: list[Mapping[str, str]] = []
        self.responses: list[CrawlHttpResponse] = []

    def get_response(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        request_headers: Mapping[str, str] | None = None,
        preserve_error_status: bool = False,
    ) -> CrawlHttpResponse:
        del allowed_hosts, preserve_error_status
        assert url == self.url
        self.requests.append(dict(request_headers or {}))
        response = CrawlHttpResponse(
            status_code=200,
            url=url,
            headers={
                "content-type": "text/html; charset=utf-8",
                "etag": '"p31-recovery"',
                "last-modified": "Mon, 03 Aug 2026 00:00:00 GMT",
            },
            content=self.body,
        )
        self.responses.append(response)
        return response


class _FailOnceEmbeddingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def embed(
        self,
        texts: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("一次性 embedding failure")
        return [[0.0] * 1536 for _ in texts]


class _StableEmbeddingProvider:
    def embed(
        self,
        texts: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        return [[0.0] * 1536 for _ in texts]


class _FailOnceAnnouncementRepository:
    def __init__(self, repository: SqlAnnouncementRepository) -> None:
        self._repository = repository
        self.calls = 0

    def upsert_incremental_announcement(self, *args: object, **kwargs: object) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("一次性公告 persistence failure")
        return self._repository.upsert_incremental_announcement(*args, **kwargs)

    def mark_incremental_source_success(self, **kwargs: object) -> None:
        self._repository.mark_incremental_source_success(**kwargs)


def _make_factory() -> tuple[sessionmaker, object]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _seed_page(
    factory: sessionmaker,
    *,
    url: str,
    unit: str,
    now: datetime,
    page_type: SitePageType = SitePageType.GENERAL_PAGE,
) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    with factory.begin() as session:
        session.add(
            SitePage(
                canonical_url=url,
                host=parsed.hostname or "",
                path=parsed.path or "/",
                title="P3.1.1 recovery page",
                unit=unit,
                page_type=page_type.value,
                crawl_status=SiteCrawlStatus.DISCOVERED.value,
                next_crawl_at=now - timedelta(minutes=1),
                is_active=True,
                is_indexable=True,
                crawl_priority=1,
            )
        )


def _site_map_sink(factory: sessionmaker, clock: _MutableClock) -> SiteMapService:
    return SiteMapService(
        SqlSiteMapRepository(factory),
        official_units=None,  # record_fetched_page does not need directory lookup
        source_configs=(),
        site_config=SiteSearchConfig(),
        clock=clock.now,
    )


def _cleanup_recovery_rows(
    factory: sessionmaker,
    *,
    page_url: str,
    document_urls: tuple[str, ...],
    announcement_urls: tuple[str, ...] = (),
    source_names: tuple[str, ...] = (),
) -> None:
    with factory.begin() as session:
        page_ids = select(SitePage.id).where(SitePage.canonical_url == page_url)
        session.execute(
            delete(SiteCrawlAttempt).where(SiteCrawlAttempt.site_page_id.in_(page_ids))
        )
        session.execute(delete(SitePage).where(SitePage.canonical_url == page_url))
        document_ids = select(Document.id).where(
            Document.canonical_url.in_(document_urls)
        )
        session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id.in_(document_ids))
        )
        session.execute(
            delete(Document).where(Document.canonical_url.in_(document_urls))
        )
        if announcement_urls:
            session.execute(
                delete(Announcement).where(
                    Announcement.canonical_url.in_(announcement_urls)
                )
            )
        if source_names:
            session.execute(delete(Source).where(Source.name.in_(source_names)))


def test_postgres_document_idempotency_is_database_backed() -> None:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    unit = f"整合測試單位-{uuid4().hex}"
    url = f"https://www.nptu.edu.tw/ingestion-idempotency-{uuid4().hex}"
    metadata = DocumentMetadata(
        title="跨 process 去重",
        source_url=url,
        unit=unit,
        published_at=date(2026, 8, 3),
        effective_from=date(2026, 8, 3),
        document_type="official_web_page",
        version="v1",
    )
    chunks = chunk_text("同一份跨 process 內容")
    embeddings = [[0.0] * 1536 for _ in chunks]
    first = SqlDocumentRepository(factory)
    second = SqlDocumentRepository(factory)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda repository: repository.save_idempotent(
                        metadata,
                        "同一份跨 process 內容",
                        chunks,
                        embeddings,
                    ),
                    (first, second),
                )
            )
        assert sorted(outcomes) == [False, True]
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.canonical_url == url)
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Document)
                    .where(
                        Document.canonical_url == url,
                        Document.is_current.is_(True),
                    )
                )
                == 1
            )
    finally:
        with factory.begin() as session:
            document_ids = select(Document.id).where(Document.canonical_url == url)
            session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id.in_(document_ids))
            )
            session.execute(delete(Document).where(Document.canonical_url == url))
        engine.dispose()


def test_postgres_announcement_source_initialization_is_database_backed() -> None:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    source_name = f"p31-source-race-{uuid4().hex}"
    source_url = "https://www.nptu.edu.tw/"
    candidates = tuple(
        AnnouncementCandidate(
            title=f"跨 process 公告 {index}",
            canonical_url=(f"https://www.nptu.edu.tw/p31-source-race-{uuid4().hex}"),
            unit="整合測試單位",
            category="測試",
            published_at=date(2026, 8, 3),
            deadline_at=None,
            body=f"公告內容 {index}",
        )
        for index in range(2)
    )

    def persist(candidate: AnnouncementCandidate) -> str:
        return SqlAnnouncementRepository(factory).upsert(
            candidate,
            source_name=source_name,
            source_url=source_url,
            interval_minutes=60,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(persist, candidates))
        assert sorted(results) == ["created", "created"]
        repository = SqlAnnouncementRepository(factory)
        latest = datetime.now(timezone.utc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda value: repository.mark_incremental_source_success(
                        source_name=source_name,
                        crawled_at=value,
                    ),
                    (latest, latest - timedelta(minutes=5)),
                )
            )
        from nptu_assistant.db.models import Source

        with factory() as session:
            assert (
                session.scalar(
                    select(Source.last_successful_crawl_at).where(
                        Source.name == source_name
                    )
                )
                == latest
            )
    finally:
        with factory.begin() as session:
            session.execute(
                delete(Document).where(
                    Document.canonical_url.in_(
                        candidate.canonical_url for candidate in candidates
                    )
                )
            )
            from nptu_assistant.db.models import Announcement, Source

            session.execute(
                delete(Announcement).where(
                    Announcement.canonical_url.in_(
                        candidate.canonical_url for candidate in candidates
                    )
                )
            )
            session.execute(delete(Source).where(Source.name == source_name))
        engine.dispose()


def test_postgres_crawl_ingestion_failure_reclaims_with_full_fetch_and_fencing() -> (
    None
):
    factory, engine = _make_factory()
    clock = _MutableClock()
    unit = f"P3.1.1 recovery unit {uuid4().hex}"
    page_url = f"https://www.nptu.edu.tw/p31-ingestion-recovery-{uuid4().hex}"
    body = """
    <html>
      <head><title>P3.1.1 復原測試頁</title></head>
      <body><h1>P3.1.1 復原測試頁</h1><p>一次性向量服務失敗後仍可恢復的完整正文。</p></body>
    </html>
    """
    _seed_page(factory, url=page_url, unit=unit, now=clock.now())
    http = _OneBodyHttpClient(page_url, body)
    embeddings = _FailOnceEmbeddingProvider()
    scheduler_repository = SqlCrawlSchedulerRepository(factory, clock=clock.now)
    scheduler = CrawlScheduler(scheduler_repository, now=clock.now)
    ingestion = CrawlIngestionService(
        SqlDocumentRepository(factory),
        embeddings,
        default_unit=unit,
    )
    crawler = IncrementalCrawler(
        http,
        _site_map_sink(factory, clock),
        scheduler=scheduler,
        ingestion_service=ingestion,
        allowed_hosts=("nptu.edu.tw",),
        max_concurrency=1,
        host_interval_seconds=0.0,
        max_active_per_host=1,
        clock=clock.monotonic,
        sleep=lambda _seconds: None,
        now=clock.now,
        worker_id=f"p31-recovery-{uuid4().hex}",
        lease_duration=timedelta(minutes=5),
        db_retry_base_seconds=0.0,
        db_retry_max_seconds=0.0,
    )

    try:
        first_run = crawler.run_once(batch_size=1)
        assert len(first_run.results) == 1
        assert first_run.results[0].outcome is IncrementalCrawlOutcome.INGESTION_FAILED
        assert embeddings.calls == 1
        with factory() as session:
            first_page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == page_url)
            )
            assert first_page is not None
            assert first_page.crawl_status == SiteCrawlStatus.FAILED.value
            assert first_page.ingestion_status == "failed"
            assert first_page.content_hash is not None
            assert first_page.ingestion_content_hash != first_page.content_hash
            assert first_page.ingestion_attempt_hash == first_page.content_hash
            assert first_page.ingestion_error is not None
            assert "一次性 embedding failure" in first_page.ingestion_error
            assert first_page.crawl_lease_owner is None
            assert first_page.crawl_lease_token is None
            assert first_page.crawl_lease_expires_at is None
            assert first_page.ingestion_lease_owner is None
            assert first_page.ingestion_lease_token is None
            assert first_page.ingestion_lease_expires_at is None
            assert first_page.next_crawl_at is not None
            assert first_page.next_crawl_at > clock.now()
            first_fetched_hash = first_page.content_hash

        clock.value += timedelta(hours=2)
        second_run = crawler.run_once(batch_size=1)
        assert len(second_run.results) == 1
        assert second_run.results[0].outcome is IncrementalCrawlOutcome.UNCHANGED
        assert embeddings.calls == 2
        assert len(http.responses) == 2
        assert http.responses[0].status_code == 200
        assert http.responses[1].status_code == 200
        assert http.responses[1].content == body.encode("utf-8")
        assert "If-None-Match" not in http.requests[1]
        assert "If-Modified-Since" not in http.requests[1]

        with factory() as session:
            recovered_page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == page_url)
            )
            assert recovered_page is not None
            assert recovered_page.crawl_status == SiteCrawlStatus.UNCHANGED.value
            assert recovered_page.ingestion_status == "success"
            assert recovered_page.content_hash == first_fetched_hash
            assert recovered_page.ingestion_content_hash == first_fetched_hash
            assert recovered_page.ingestion_error is None
            assert recovered_page.crawl_lease_owner is None
            assert recovered_page.crawl_lease_token is None
            assert recovered_page.crawl_lease_expires_at is None
            assert recovered_page.ingestion_lease_owner is None
            assert recovered_page.ingestion_lease_token is None
            assert recovered_page.ingestion_lease_expires_at is None
            documents = session.scalars(
                select(Document).where(Document.canonical_url == page_url)
            ).all()
            assert len(documents) == 1
            assert documents[0].is_current is True
            assert documents[0].content_hash == first_fetched_hash
            assert len(documents[0].chunks) > 0
            attempts = session.scalars(
                select(SiteCrawlAttempt)
                .where(SiteCrawlAttempt.site_page_id == recovered_page.id)
                .order_by(SiteCrawlAttempt.started_at, SiteCrawlAttempt.created_at)
            ).all()
            assert [attempt.outcome for attempt in attempts] == [
                "failed_transient",
                "success_unchanged",
            ]

        with factory.begin() as session:
            page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == page_url)
            )
            assert page is not None
            page.next_crawl_at = clock.now() - timedelta(seconds=1)
        stale_claim = scheduler.claim_due(
            owner=f"p31-stale-{uuid4().hex}",
            limit=1,
            lease_duration=timedelta(seconds=1),
            urls=(page_url,),
        )[0]
        clock.value += timedelta(seconds=2)
        fresh_claim = scheduler.claim_due(
            owner=f"p31-fresh-{uuid4().hex}",
            limit=1,
            lease_duration=timedelta(minutes=5),
            urls=(page_url,),
        )[0]
        assert stale_claim.token != fresh_claim.token
        assert scheduler.complete(stale_claim, changed=True) is False
        with factory() as session:
            fenced_page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == page_url)
            )
            assert fenced_page is not None
            assert fenced_page.crawl_lease_token == fresh_claim.token
            assert fenced_page.content_hash == first_fetched_hash
        assert scheduler.complete(fresh_claim, changed=False) is True
        with factory() as session:
            attempts = session.scalars(
                select(SiteCrawlAttempt)
                .join(SitePage)
                .where(SitePage.canonical_url == page_url)
                .order_by(SiteCrawlAttempt.started_at, SiteCrawlAttempt.created_at)
            ).all()
            assert attempts[-2].outcome == "lease_lost"
            assert attempts[-1].outcome == "success_unchanged"
    finally:
        _cleanup_recovery_rows(
            factory,
            page_url=page_url,
            document_urls=(page_url,),
            source_names=(f"document:{unit}",),
        )
        engine.dispose()


def test_postgres_document_success_announcement_failure_retries_partial_state() -> None:
    factory, engine = _make_factory()
    clock = _MutableClock()
    unit = f"P3.1.1 partial unit {uuid4().hex}"
    page_url = f"https://www.nptu.edu.tw/p31-partial-page-{uuid4().hex}"
    announcement_url = f"https://www.nptu.edu.tw/p31-partial-announcement-{uuid4().hex}"
    source_name = f"p31-partial-source-{uuid4().hex}"
    page = NptuSitePage(
        title="P3.1.1 公告列表",
        canonical_url=page_url,
        body="公告列表頁正文，文件 ingestion 應先成功，公告 persistence 再單獨失敗。",
        published_at=None,
        links=(announcement_url,),
        role=UnitAnnouncementPageRole.LISTING,
        announcement_items=(
            NptuListingItem(
                title="可恢復的公告",
                canonical_url=announcement_url,
                published_at=date(2026, 8, 3),
                summary="公告摘要",
                anchor_text="可恢復的公告",
                order=0,
            ),
        ),
    )
    source = AnnouncementSourceIdentity(
        name=source_name,
        url=f"https://www.nptu.edu.tw/{source_name}",
        unit=unit,
    )
    _seed_page(
        factory,
        url=page_url,
        unit=unit,
        now=clock.now(),
        page_type=SitePageType.ANNOUNCEMENT_LISTING,
    )
    announcement_repository = _FailOnceAnnouncementRepository(
        SqlAnnouncementRepository(factory)
    )
    ingestion = CrawlIngestionService(
        SqlDocumentRepository(factory),
        _StableEmbeddingProvider(),
        default_unit=unit,
        announcement_adapter=IncrementalAnnouncementAdapter(announcement_repository),
        announcement_source_resolver=lambda _page, _unit: source,
    )
    scheduler_repository = SqlCrawlSchedulerRepository(factory, clock=clock.now)
    scheduler = CrawlScheduler(scheduler_repository, now=clock.now)

    try:
        first_claim = scheduler.claim_due(
            owner=f"p31-partial-first-{uuid4().hex}",
            limit=1,
            lease_duration=timedelta(minutes=5),
            urls=(page_url,),
        )[0]
        first_result = ingestion.ingest_page(
            page,
            unit=unit,
            page_id=first_claim.page_id,
            lease_owner=first_claim.owner,
            lease_token=first_claim.token,
            lease_expires_at=first_claim.lease_expires_at,
        )
        assert first_result.status is CrawlIngestionStatus.PARTIAL
        assert first_result.announcement_persisted == 0
        assert first_result.announcement_failed == 1
        assert (
            scheduler.fail(
                first_claim,
                error_kind="ingestion_failed",
                error_message=first_result.error,
                ingestion_performed=True,
            ).applied
            is True
        )

        expected_hash = content_hash(page.body.strip())
        with factory() as session:
            partial_page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == page_url)
            )
            assert partial_page is not None
            assert partial_page.ingestion_status == "partial"
            assert partial_page.announcement_ingestion_status == "failed"
            assert partial_page.ingestion_content_hash == expected_hash
            assert partial_page.announcement_ingestion_error is not None
            assert "公告持久化部分成功" in partial_page.announcement_ingestion_error
            assert partial_page.ingestion_lease_owner is None
            assert partial_page.ingestion_lease_token is None
            assert partial_page.ingestion_lease_expires_at is None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.canonical_url == page_url)
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Announcement)
                    .where(Announcement.canonical_url == announcement_url)
                )
                == 0
            )

        clock.value += timedelta(minutes=5)
        second_claim = scheduler.claim_due(
            owner=f"p31-partial-second-{uuid4().hex}",
            limit=1,
            lease_duration=timedelta(minutes=5),
            urls=(page_url,),
        )[0]
        second_result = ingestion.ingest_page(
            page,
            unit=unit,
            page_id=second_claim.page_id,
            lease_owner=second_claim.owner,
            lease_token=second_claim.token,
            lease_expires_at=second_claim.lease_expires_at,
        )
        assert second_result.status is CrawlIngestionStatus.SKIPPED
        assert second_result.announcement_persisted == 1
        assert second_result.announcement_failed == 0
        assert scheduler.complete(second_claim, changed=False) is True

        with factory() as session:
            recovered_page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == page_url)
            )
            assert recovered_page is not None
            assert recovered_page.ingestion_status == "success"
            assert recovered_page.announcement_ingestion_status == "success"
            assert recovered_page.ingestion_content_hash == expected_hash
            assert recovered_page.ingestion_error is None
            assert recovered_page.announcement_ingestion_error is None
            documents = session.scalars(
                select(Document).where(Document.canonical_url == page_url)
            ).all()
            assert len(documents) == 1
            assert documents[0].is_current is True
            assert len(documents[0].chunks) > 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Announcement)
                    .where(Announcement.canonical_url == announcement_url)
                )
                == 1
            )
            assert announcement_repository.calls == 2
    finally:
        _cleanup_recovery_rows(
            factory,
            page_url=page_url,
            document_urls=(page_url,),
            announcement_urls=(announcement_url,),
            source_names=(f"document:{unit}", source_name),
        )
        engine.dispose()
