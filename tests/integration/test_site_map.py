from __future__ import annotations

from collections.abc import Collection
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
import uuid

import pytest
from sqlalchemy import create_engine, delete, event, func, inspect, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.official_units import load_default_official_unit_directory
from nptu_assistant.crawlers.site_map import (
    SiteDiscoverySource,
    SiteLinkType,
    SiteLinkUpsert,
    SiteMapService,
    SitePageType,
    SitePageUpsert,
)
from nptu_assistant.crawlers.site_models import (
    DiscoveredPage,
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchPlan,
)
from nptu_assistant.crawlers.site_search import NptuSiteSearchService
from nptu_assistant.crawlers.site_search_cache import InMemorySiteSearchCache
from nptu_assistant.db.models import Announcement, Document, SiteLink, SitePage, Source
from nptu_assistant.db.site_map import SqlSiteMapRepository


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires a migrated PostgreSQL database with pgvector and pg_trgm",
)


def make_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def cleanup(factory: sessionmaker[Session], prefix: str) -> None:
    with factory.begin() as session:
        session.execute(
            delete(Announcement).where(Announcement.canonical_url.like(f"{prefix}%"))
        )
        session.execute(
            delete(Document).where(Document.canonical_url.like(f"{prefix}%"))
        )
        session.execute(delete(Source).where(Source.name.like(f"{prefix}%")))
        session.execute(
            delete(SitePage).where(SitePage.canonical_url.like(f"{prefix}%"))
        )


def test_site_map_schema_has_constraints_and_indexes() -> None:
    factory, engine = make_factory()
    try:
        database_inspector = inspect(engine)
        assert {"site_pages", "site_links"} <= set(database_inspector.get_table_names())
        site_page_indexes = {
            item["name"] for item in database_inspector.get_indexes("site_pages")
        }
        assert {
            "ix_site_pages_host",
            "ix_site_pages_unit",
            "ix_site_pages_page_type",
            "ix_site_pages_crawl_status",
            "ix_site_pages_next_crawl_at",
            "ix_site_pages_active_indexable",
            "ix_site_pages_host_priority",
        } <= site_page_indexes
        assert "ix_site_links_anchor_text_trgm" in {
            item["name"] for item in database_inspector.get_indexes("site_links")
        }
        foreign_keys = database_inspector.get_foreign_keys("site_links")
        assert {item["referred_table"] for item in foreign_keys} == {"site_pages"}
        uniques = database_inspector.get_unique_constraints("site_links")
        assert any(item["name"] == "uq_site_links_source_target" for item in uniques)
    finally:
        engine.dispose()


def test_concurrent_page_and_link_upsert_is_idempotent() -> None:
    factory, engine = make_factory()
    prefix = f"https://www.nptu.edu.tw/p2-concurrent-{uuid.uuid4().hex}"
    source = SitePageUpsert(
        canonical_url=f"{prefix}/source",
        title="來源",
        page_type=SitePageType.GENERAL_PAGE,
        discovery_source=SiteDiscoverySource.CONFIGURED_SEED,
    )
    target = SitePageUpsert(
        canonical_url=f"{prefix}/target",
        title="目標",
        page_type=SitePageType.UNKNOWN,
        discovery_source=SiteDiscoverySource.INTERNAL_LINK,
    )
    try:
        repository = SqlSiteMapRepository(factory)
        with ThreadPoolExecutor(max_workers=4) as pool:
            page_results = list(
                pool.map(lambda _: repository.upsert_page(source), range(8))
            )
        assert sum(result.created for result in page_results) == 1
        assert sum(result.updated for result in page_results) == 7
        with ThreadPoolExecutor(max_workers=4) as pool:
            link_results = list(
                pool.map(
                    lambda _: repository.upsert_link(
                        source,
                        target,
                        anchor_text="目標頁",
                        link_type=SiteLinkType.CONTENT,
                    ),
                    range(8),
                )
            )
        assert sum(result.created for result in link_results) == 1
        assert sum(result.updated for result in link_results) == 7
        with factory() as session:
            assert (
                session.scalar(
                    select(SitePage.id).where(
                        SitePage.canonical_url == source.canonical_url
                    )
                )
                is not None
            )
            assert (
                session.scalar(
                    select(SitePage.id).where(
                        SitePage.canonical_url == target.canonical_url
                    )
                )
                is not None
            )
            source_id = session.scalar(
                select(SitePage.id).where(
                    SitePage.canonical_url == source.canonical_url
                )
            )
            target_id = session.scalar(
                select(SitePage.id).where(
                    SitePage.canonical_url == target.canonical_url
                )
            )
            assert source_id is not None
            assert target_id is not None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SiteLink)
                    .where(
                        SiteLink.source_page_id == source_id,
                        SiteLink.target_page_id == target_id,
                    )
                )
                == 1
            )
    finally:
        cleanup(factory, prefix)
        engine.dispose()


def test_batch_persistence_100_links_uses_one_transaction_and_fixed_sql() -> None:
    factory, engine = make_factory()
    token = uuid.uuid4().hex
    prefix = f"https://www.nptu.edu.tw/p2-batch-{token}"
    source = SitePageUpsert(
        canonical_url=f"{prefix}/source",
        title="批次來源",
        page_type=SitePageType.GENERAL_PAGE,
        discovery_source=SiteDiscoverySource.INTERNAL_LINK,
    )
    links = tuple(
        SiteLinkUpsert(
            target=SitePageUpsert(
                canonical_url=f"{prefix}/target-{index}",
                title=f"目標 {index}",
                discovery_source=SiteDiscoverySource.INTERNAL_LINK,
            ),
            anchor_text=f"目標連結 {index}",
            link_type=SiteLinkType.CONTENT,
        )
        for index in range(100)
    )
    statements: list[str] = []

    def capture_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        started = perf_counter()
        result = SqlSiteMapRepository(factory).persist_fetched_page(
            source,
            title=source.title,
            content_hash="c" * 64,
            http_status=200,
            links=links,
        )
        batch_ms = (perf_counter() - started) * 1000
        batch_statement_count = len(statements)
        assert result.links_created == 100
        assert result.statement_count <= 8
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(SitePage.canonical_url.like(f"{prefix}%"))
                )
                == 101
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SiteLink)
                    .join(SiteLink.source_page)
                    .where(SitePage.canonical_url == f"{prefix}/source")
                )
                == 100
            )
        assert len(statements) <= 8
        print(
            "site_map_batch_benchmark "
            f"links=100 transactions=1 statements={batch_statement_count} "
            f"elapsed_ms={batch_ms:.2f}"
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        cleanup(factory, prefix)
        engine.dispose()


def test_candidate_lexical_relevance_uses_concepts_and_incoming_anchor() -> None:
    factory, engine = make_factory()
    token = uuid.uuid4().hex
    prefix = f"https://www.nptu.edu.tw/p2-lexical-{token}"
    relevant = SitePageUpsert(
        canonical_url=f"{prefix}/aid",
        title="弱勢學生助學計畫",
        page_type=SitePageType.GENERAL_PAGE,
        discovery_source=SiteDiscoverySource.INTERNAL_LINK,
    )
    home = SitePageUpsert(
        canonical_url=f"{prefix}/",
        title="行政單位首頁",
        page_type=SitePageType.UNIT_HOMEPAGE,
        discovery_source=SiteDiscoverySource.OFFICIAL_UNIT,
        crawl_priority=100,
    )
    try:
        repository = SqlSiteMapRepository(factory)
        repository.upsert_page(relevant)
        repository.upsert_page(home)
        repository.upsert_link(
            home,
            relevant,
            anchor_text="低收入戶及中低收入戶助學金申請",
            link_type=SiteLinkType.CONTENT,
        )
        plan = SearchPlan(
            query="我是低收入戶，要怎麼申請住宿補助？",
            search_queries=["低收入戶住宿補助"],
            concepts=["低收入戶及中低收入戶助學金申請", "弱勢學生助學計畫"],
            limit=2,
        )
        candidates = repository.find_candidates(
            plan,
            scope=None,
            allowed_hosts=("nptu.edu.tw",),
            limit=2,
        )
        assert candidates[0].canonical_url == relevant.canonical_url
        assert candidates[0].lexical_relevance > candidates[1].lexical_relevance
    finally:
        cleanup(factory, prefix)
        engine.dispose()


def test_expired_site_map_deadline_executes_no_sql() -> None:
    factory, engine = make_factory()
    statements: list[str] = []

    def capture_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        deadline = SearchDeadline(expires_at=1.0, _clock=lambda: 2.0)
        with pytest.raises(SearchDeadlineExceeded):
            SqlSiteMapRepository(factory).find_candidates(
                SearchPlan.from_query("宿舍冷氣費", limit=1),
                scope=None,
                allowed_hosts=("nptu.edu.tw",),
                limit=1,
                deadline=deadline,
            )
        assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        engine.dispose()


def test_site_map_deadline_sets_transaction_local_statement_timeout() -> None:
    factory, engine = make_factory()
    statements: list[tuple[str, object]] = []

    def capture_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, context, executemany
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        deadline = SearchDeadline(expires_at=12.345, _clock=lambda: 0.0)
        SqlSiteMapRepository(factory).find_candidates(
            SearchPlan.from_query("宿舍冷氣費", limit=1),
            scope=None,
            allowed_hosts=("nptu.edu.tw",),
            limit=1,
            deadline=deadline,
        )
        timeout_calls = [
            (statement, parameters)
            for statement, parameters in statements
            if "set_config" in statement
        ]
        assert len(timeout_calls) == 1
        assert "12345ms" in repr(timeout_calls[0][1])
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        engine.dispose()


def test_candidate_lookup_benchmark_100_and_5000_pages() -> None:
    factory, engine = make_factory()
    token = uuid.uuid4().hex
    prefix = f"https://www.nptu.edu.tw/p2-benchmark-{token}"
    now = datetime.now(timezone.utc)

    def page_rows(start: int, count: int) -> list[dict[str, object]]:
        return [
            {
                "id": uuid.uuid4(),
                "canonical_url": f"{prefix}/page-{index}",
                "host": "www.nptu.edu.tw",
                "path": f"/page-{index}",
                "title": "宿舍冷氣費計算" if index == 0 else f"一般校務頁面 {index}",
                "unit": None,
                "page_type": SitePageType.GENERAL_PAGE.value,
                "discovery_source": SiteDiscoverySource.MANUAL.value,
                "crawl_status": "success",
                "last_discovered_at": now,
                "last_successful_crawl_at": now,
                "crawl_priority": 40,
                "minimum_depth": 0,
                "failure_count": 0,
                "is_indexable": True,
                "is_active": True,
            }
            for index in range(start, start + count)
        ]

    try:
        with factory.begin() as session:
            session.execute(SitePage.__table__.insert(), page_rows(0, 100))
        repository = SqlSiteMapRepository(factory)
        plan = SearchPlan.from_query("宿舍冷氣費", limit=5)
        started = perf_counter()
        small = repository.find_candidates(
            plan, scope=None, allowed_hosts=("nptu.edu.tw",), limit=5
        )
        small_ms = (perf_counter() - started) * 1000
        assert small

        with factory.begin() as session:
            session.execute(SitePage.__table__.insert(), page_rows(100, 4_900))
            page_ids = dict(
                session.execute(
                    select(SitePage.canonical_url, SitePage.id).where(
                        SitePage.canonical_url.like(f"{prefix}/page-%")
                    )
                ).all()
            )
            edge_rows = [
                {
                    "id": uuid.uuid4(),
                    "source_page_id": page_ids[f"{prefix}/page-{index}"],
                    "target_page_id": page_ids[
                        f"{prefix}/page-{(index + offset) % 5_000}"
                    ],
                    "anchor_text": "宿舍冷氣費計算" if index == 0 else "一般連結",
                    "link_type": SiteLinkType.CONTENT.value,
                    "first_discovered_at": now,
                    "last_discovered_at": now,
                }
                for index in range(5_000)
                for offset in range(1, 5)
            ]
            session.execute(SiteLink.__table__.insert(), edge_rows)
        started = perf_counter()
        medium = repository.find_candidates(
            plan, scope=None, allowed_hosts=("nptu.edu.tw",), limit=5
        )
        medium_ms = (perf_counter() - started) * 1000
        assert medium

        with factory() as session:
            explain_rows = session.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) "
                    "SELECT id FROM site_pages "
                    "WHERE title % CAST(:query AS text) "
                    "OR path % CAST(:query AS text) "
                    "LIMIT 5"
                ),
                {"query": "宿舍冷氣費"},
            ).scalars()
            explain = tuple(explain_rows)
        assert explain
        print(
            "site_map_benchmark "
            f"small_pages=100 small_ms={small_ms:.2f} "
            f"medium_pages=5000 medium_links=20000 medium_ms={medium_ms:.2f} "
            f"explain={explain[0]}"
        )
    finally:
        cleanup(factory, prefix)
        engine.dispose()


class RecordingDiscovery:
    def __init__(self, url: str, title: str) -> None:
        self.url = url
        self.title = title
        self.calls = 0

    def discover(
        self,
        plan: SearchPlan,
        *,
        max_items: int,
        deadline: SearchDeadline,
    ) -> tuple[DiscoveredPage, ...]:
        del plan, max_items
        self.calls += 1
        deadline.raise_if_expired()
        return (DiscoveredPage(self.url, self.title, 1.0),)


class MappingHttpClient:
    def __init__(self, title: str) -> None:
        self.title = title
        self.calls = 0

    def get(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        del allowed_hosts, timeout_seconds
        self.calls += 1
        if deadline is not None:
            deadline.raise_if_expired()
        return f"<html><title>{self.title}</title><body>{self.title} 內容</body></html>"


class DeterministicScorer:
    def score_candidate(self, plan: SearchPlan, candidate: object) -> float:
        del plan, candidate
        return 1.0

    def score_pages(
        self, plan: SearchPlan, candidates: object, pages: object, **kwargs: object
    ) -> list[float]:
        del plan, candidates, kwargs
        return [1.0 for _page in pages]  # type: ignore[union-attr]


class IncrementingClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.current += timedelta(microseconds=1)
        return self.current


def make_site_config() -> SiteSearchConfig:
    return SiteSearchConfig(
        enabled=True,
        seed_urls=["https://www.nptu.edu.tw/"],
        allowed_hosts=["nptu.edu.tw"],
        max_items=1,
        max_candidate_urls=5,
        max_pages=1,
        max_depth=0,
        max_pages_per_host=2,
        early_stop_min_results=1,
    )


def make_map_service(repository: SqlSiteMapRepository) -> SiteMapService:
    return SiteMapService(
        repository,
        official_units=load_default_official_unit_directory(),
        source_configs=(),
        site_config=make_site_config(),
    )


def test_second_service_instance_uses_persisted_map_before_discovery() -> None:
    factory, engine = make_factory()
    token = uuid.uuid4().hex
    prefix = f"https://www.nptu.edu.tw/p2-cross-instance-{token}"
    title = f"site map {token}"
    plan = SearchPlan.from_query(title, limit=1)
    first_discovery = RecordingDiscovery(prefix, title)
    second_discovery = RecordingDiscovery(prefix, title)
    try:
        first_http = MappingHttpClient(title)
        first_map = make_map_service(SqlSiteMapRepository(factory))
        first = NptuSiteSearchService(
            make_site_config(),
            first_http,
            scorer=DeterministicScorer(),  # type: ignore[arg-type]
            discovery=first_discovery,
            cache=InMemorySiteSearchCache(),
            site_map=first_map,
        )
        first_result = first.search(plan)
        assert first_result.pages
        assert first_discovery.calls == 1

        second_http = MappingHttpClient(title)
        second_map = make_map_service(SqlSiteMapRepository(factory))
        second = NptuSiteSearchService(
            make_site_config(),
            second_http,
            scorer=DeterministicScorer(),  # type: ignore[arg-type]
            discovery=second_discovery,
            cache=InMemorySiteSearchCache(),
            site_map=second_map,
        )
        second_result = second.search(plan)
        assert second_result.pages
        assert second_discovery.calls == 0
        assert second_http.calls == 1
        assert (
            second_result.pages[0].canonical_url == first_result.pages[0].canonical_url
        )
    finally:
        cleanup(factory, prefix)
        engine.dispose()


def test_crawl_state_tracks_hash_changes_and_failure_recovery() -> None:
    factory, engine = make_factory()
    prefix = f"https://www.nptu.edu.tw/p2-state-{uuid.uuid4().hex}"
    url = f"{prefix}/page"
    try:
        repository = SqlSiteMapRepository(factory, clock=IncrementingClock())
        repository.upsert_page(SitePageUpsert(canonical_url=url))
        repository.record_crawl_success(
            url,
            title="第一版",
            content_hash="a" * 64,
            http_status=200,
            etag='"v1"',
            last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
        )
        with factory() as session:
            first = session.scalar(
                select(SitePage).where(SitePage.canonical_url == url)
            )
            assert first is not None
            first_changed_at = first.last_changed_at
            assert first.crawl_status == "success"
            assert first.failure_count == 0
            assert first.etag == '"v1"'
        repository.record_crawl_success(
            url,
            title="第一版",
            content_hash="a" * 64,
            http_status=200,
        )
        with factory() as session:
            unchanged = session.scalar(
                select(SitePage).where(SitePage.canonical_url == url)
            )
            assert unchanged is not None
            assert unchanged.crawl_status == "unchanged"
            assert unchanged.last_changed_at == first_changed_at
        repository.record_crawl_failure(url, http_status=503)
        repository.record_crawl_success(
            url,
            title="第二版",
            content_hash="b" * 64,
            http_status=200,
        )
        with factory() as session:
            recovered = session.scalar(
                select(SitePage).where(SitePage.canonical_url == url)
            )
            assert recovered is not None
            assert recovered.crawl_status == "success"
            assert recovered.failure_count == 0
            assert recovered.content_hash == "b" * 64
            assert recovered.last_changed_at != first_changed_at
    finally:
        cleanup(factory, prefix)
        engine.dispose()


def test_existing_source_document_announcement_urls_bootstrap_idempotently() -> None:
    factory, engine = make_factory()
    token = uuid.uuid4().hex
    prefix = f"https://www.nptu.edu.tw/p2-bootstrap-{token}"
    source_name = f"{prefix}-source"
    try:
        with factory.begin() as session:
            source = Source(
                name=source_name,
                base_url=f"{prefix}/base",
                unit="國立屏東大學",
                source_type="announcement",
                canonical_urls=[f"{prefix}/configured"],
            )
            session.add(source)
            session.flush()
            session.add(
                Document(
                    source_id=source.id,
                    title="官方文件",
                    canonical_url=f"{prefix}/document",
                    document_type="policy",
                    version="1",
                    content_hash="a" * 64,
                    raw_text="文件內容",
                    is_current=True,
                )
            )
            session.add(
                Announcement(
                    source_id=source.id,
                    title="公告",
                    unit="國立屏東大學",
                    published_at=date(2026, 1, 1),
                    canonical_url=f"{prefix}/announcement",
                    body="公告內容",
                    content_hash="b" * 64,
                    last_crawled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )
            )
        repository = SqlSiteMapRepository(factory)
        first = repository.import_existing_urls()
        second = repository.import_existing_urls()
        with factory() as session:
            rows = session.scalars(
                select(SitePage).where(SitePage.canonical_url.like(f"{prefix}%"))
            ).all()
            assert len(rows) == 4
            assert {item.page_type for item in rows} >= {
                SitePageType.ANNOUNCEMENT_LISTING.value,
                SitePageType.OFFICIAL_DOCUMENT.value,
                SitePageType.ANNOUNCEMENT_DETAIL.value,
            }
        assert first["Document URLs"].created >= 1
        assert second["Document URLs"].created == 0
        assert second["Document URLs"].updated >= 1
    finally:
        cleanup(factory, prefix)
        engine.dispose()


def test_bootstrap_imports_only_current_documents_and_deactivates_stale_rows() -> None:
    factory, engine = make_factory()
    token = uuid.uuid4().hex
    prefix = f"https://www.nptu.edu.tw/p2-current-doc-{token}"
    source_name = f"{prefix}-source"
    current_url = f"{prefix}/current"
    superseded_url = f"{prefix}/superseded"
    try:
        with factory.begin() as session:
            source = Source(
                name=source_name,
                base_url=f"{prefix}/base",
                unit="國立屏東大學",
                source_type="document",
                canonical_urls=[],
            )
            session.add(source)
            session.flush()
            session.add_all(
                [
                    Document(
                        source_id=source.id,
                        title="目前文件",
                        canonical_url=current_url,
                        document_type="policy",
                        version="2",
                        content_hash="a" * 64,
                        raw_text="目前內容",
                        is_current=True,
                    ),
                    Document(
                        source_id=source.id,
                        title="舊文件",
                        canonical_url=superseded_url,
                        document_type="policy",
                        version="1",
                        content_hash="b" * 64,
                        raw_text="舊內容",
                        is_current=False,
                    ),
                ]
            )
        repository = SqlSiteMapRepository(factory)
        first = repository.import_existing_urls()
        with factory() as session:
            rows = {
                row.canonical_url: row
                for row in session.scalars(
                    select(SitePage).where(SitePage.canonical_url.like(f"{prefix}%"))
                ).all()
            }
            assert current_url in rows
            assert superseded_url not in rows
        assert first["Document URLs"].created == 1

        with factory.begin() as session:
            session.execute(
                update(Document)
                .where(Document.canonical_url == current_url)
                .values(is_current=False)
            )
        repository.import_existing_urls()
        with factory() as session:
            stale = session.scalar(
                select(SitePage).where(SitePage.canonical_url == current_url)
            )
            assert stale is not None
            assert stale.is_active is False
            assert stale.is_indexable is False
    finally:
        cleanup(factory, prefix)
        engine.dispose()
