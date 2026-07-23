from __future__ import annotations

from collections.abc import Collection
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine, delete, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.official_units import load_default_official_unit_directory
from nptu_assistant.crawlers.site_map import (
    SiteDiscoverySource,
    SiteLinkType,
    SiteMapService,
    SitePageType,
    SitePageUpsert,
)
from nptu_assistant.crawlers.site_models import DiscoveredPage, SearchDeadline, SearchPlan
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
        session.execute(delete(SitePage).where(SitePage.canonical_url.like(f"{prefix}%")))


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
        foreign_keys = database_inspector.get_foreign_keys("site_links")
        assert {item["referred_table"] for item in foreign_keys} == {"site_pages"}
        uniques = database_inspector.get_unique_constraints("site_links")
        assert any(
            item["name"] == "uq_site_links_source_target" for item in uniques
        )
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
            list(pool.map(lambda _: repository.upsert_page(source), range(8)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
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
        with factory() as session:
            assert (
                session.scalar(
                    select(SitePage.id).where(SitePage.canonical_url == source.canonical_url)
                )
                is not None
            )
            assert session.scalar(select(SitePage.id).where(SitePage.canonical_url == target.canonical_url)) is not None
            assert session.scalar(select(SiteLink.id)) is not None
            assert session.scalar(select(func.count()).select_from(SiteLink)) == 1
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

    def score_pages(self, plan: SearchPlan, candidates: object, pages: object, **kwargs: object) -> list[float]:
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
        assert second_result.pages[0].canonical_url == first_result.pages[0].canonical_url
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
            first = session.scalar(select(SitePage).where(SitePage.canonical_url == url))
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
            unchanged = session.scalar(select(SitePage).where(SitePage.canonical_url == url))
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
            recovered = session.scalar(select(SitePage).where(SitePage.canonical_url == url))
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
        assert first["Document URLs"].created == 1
        assert second["Document URLs"].created == 0
        assert second["Document URLs"].updated >= 1
    finally:
        cleanup(factory, prefix)
        engine.dispose()
