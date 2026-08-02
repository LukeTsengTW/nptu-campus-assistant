from __future__ import annotations

import os
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from nptu_assistant.crawlers.site_map import SiteCrawlStatus, SitePageType
from nptu_assistant.db.crawl_models import SiteCrawlAttempt
from nptu_assistant.db.crawl_scheduler import SqlCrawlSchedulerRepository
from nptu_assistant.db.models import SitePage
from sqlalchemy import create_engine, delete, event, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires a migrated PostgreSQL database",
)


def make_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def seed_pages(factory: sessionmaker[Session], prefix: str, count: int) -> None:
    now = datetime.now(timezone.utc)
    with factory.begin() as session:
        session.add_all(
            [
                SitePage(
                    canonical_url=f"{prefix}/{index}",
                    host="www.nptu.edu.tw",
                    path=f"/{index}",
                    title=f"P3 page {index}",
                    page_type=SitePageType.GENERAL_PAGE.value,
                    crawl_status=SiteCrawlStatus.DISCOVERED.value,
                    next_crawl_at=now - timedelta(minutes=1),
                    is_active=True,
                    is_indexable=True,
                    crawl_priority=1,
                )
                for index in range(count)
            ]
        )


def cleanup(factory: sessionmaker[Session], prefix: str) -> None:
    with factory.begin() as session:
        page_ids = select(SitePage.id).where(SitePage.canonical_url.like(f"{prefix}%"))
        session.execute(
            delete(SiteCrawlAttempt).where(SiteCrawlAttempt.site_page_id.in_(page_ids))
        )
        session.execute(
            delete(SitePage).where(SitePage.canonical_url.like(f"{prefix}%"))
        )


def test_postgres_claim_is_unique_and_bounded_sql() -> None:
    factory, engine = make_factory()
    prefix = f"https://www.nptu.edu.tw/p3-claim-{uuid.uuid4().hex}"
    seed_pages(factory, prefix, 100)
    statements: list[str] = []

    def capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.strip():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        repository = SqlCrawlSchedulerRepository(factory)

        def claim(worker: int):
            return repository.claim_due(
                owner=f"p3-worker-{worker}",
                limit=25,
                lease_duration=timedelta(minutes=5),
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = [
                claim_result
                for result in pool.map(claim, range(4))
                for claim_result in result
            ]

        assert len(claims) == 100
        assert len({claim.page_id for claim in claims}) == 100
        assert (
            sum("UPDATE SITE_PAGES" in statement.upper() for statement in statements)
            == 4
        )
        assert len(statements) <= 12
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SiteCrawlAttempt)
                    .where(SiteCrawlAttempt.worker_id.like("p3-worker-%"))
                )
                == 100
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        cleanup(factory, prefix)
        engine.dispose()


def test_expired_lease_fences_stale_completion_and_records_attempt() -> None:
    factory, engine = make_factory()
    prefix = f"https://www.nptu.edu.tw/p3-expiry-{uuid.uuid4().hex}"
    seed_pages(factory, prefix, 1)
    now = datetime.now(timezone.utc)
    repository = SqlCrawlSchedulerRepository(factory)
    try:
        first = repository.claim_due(
            owner="p3-worker-a",
            limit=1,
            lease_duration=timedelta(seconds=1),
            now=now,
        )[0]
        second = repository.claim_due(
            owner="p3-worker-b",
            limit=1,
            lease_duration=timedelta(minutes=5),
            now=now + timedelta(seconds=2),
        )[0]
        assert first.page_id == second.page_id
        assert not repository.complete(
            first,
            crawl_status=SiteCrawlStatus.SUCCESS.value,
            next_crawl_at=now + timedelta(hours=1),
            now=now + timedelta(seconds=2),
            content_changed=True,
        )
        assert repository.complete(
            second,
            crawl_status=SiteCrawlStatus.SUCCESS.value,
            next_crawl_at=now + timedelta(hours=1),
            now=now + timedelta(seconds=2),
            content_changed=True,
        )
        with factory() as session:
            attempt_outcomes = session.scalars(
                select(SiteCrawlAttempt.outcome).where(
                    SiteCrawlAttempt.site_page_id == second.page_id
                )
            ).all()
            assert Counter(attempt_outcomes)["success_changed"] == 1
            assert (
                session.scalar(
                    select(SitePage.crawl_status).where(SitePage.id == second.page_id)
                )
                == SiteCrawlStatus.SUCCESS.value
            )
    finally:
        cleanup(factory, prefix)
        engine.dispose()


def test_p3_migration_columns_and_indexes_are_live() -> None:
    _factory, engine = make_factory()
    try:
        page_columns = {
            column["name"] for column in inspect(engine).get_columns("site_pages")
        }
        attempt_columns = {
            column["name"]
            for column in inspect(engine).get_columns("site_crawl_attempts")
        }
        assert {
            "crawl_lease_owner",
            "crawl_lease_token",
            "crawl_lease_expires_at",
            "last_scheduled_at",
            "unchanged_streak",
            "changed_streak",
            "last_error_kind",
            "last_error_at",
            "last_retry_after_at",
        } <= page_columns
        assert {
            "worker_id",
            "duration_ms",
            "content_changed",
            "links_discovered",
            "ingestion_performed",
            "error_kind",
            "error_message",
            "created_at",
        } <= attempt_columns
        index_names = {
            item["name"] for item in inspect(engine).get_indexes("site_pages")
        }
        assert {
            "ix_site_pages_crawl_schedule",
            "ix_site_pages_host_next_crawl_at",
            "ix_site_pages_crawl_lease_expires_at",
            "ix_site_pages_due_active_crawlable",
        } <= index_names
    finally:
        engine.dispose()
