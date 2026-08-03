from __future__ import annotations

import os
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from nptu_assistant.crawlers.site_map import SiteCrawlStatus, SitePageType
from nptu_assistant.db.crawl_models import SiteCrawlAttempt
from nptu_assistant.db.crawl_scheduler import SqlCrawlSchedulerRepository
from nptu_assistant.db.models import SitePage
from sqlalchemy import create_engine, delete, event, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires a migrated PostgreSQL database",
)


def make_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def seed_pages(
    factory: sessionmaker[Session],
    prefix: str,
    count: int,
    *,
    distinct_hosts: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    token = prefix.rsplit("/", 1)[-1]
    shared_host = f"p3-{token}.nptu.edu.tw"
    with factory.begin() as session:
        session.add_all(
            [
                SitePage(
                    canonical_url=(
                        f"https://p3-{token}-{index}.nptu.edu.tw/{index}"
                        if distinct_hosts
                        else f"{prefix}/{index}"
                    ),
                    host=(
                        f"p3-{token}-{index}.nptu.edu.tw"
                        if distinct_hosts
                        else shared_host
                    ),
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


def cleanup(
    factory: sessionmaker[Session],
    prefix: str,
    *,
    distinct_hosts: bool = False,
) -> None:
    url_filter = (
        SitePage.host.like(f"p3-{prefix.rsplit('/', 1)[-1]}-%")
        if distinct_hosts
        else SitePage.canonical_url.like(f"{prefix}%")
    )
    with factory.begin() as session:
        page_ids = select(SitePage.id).where(url_filter)
        session.execute(
            delete(SiteCrawlAttempt).where(SiteCrawlAttempt.site_page_id.in_(page_ids))
        )
        session.execute(delete(SitePage).where(url_filter))


def test_postgres_claim_is_unique_and_bounded_sql() -> None:
    factory, engine = make_factory()
    prefix = f"https://www.nptu.edu.tw/p3-claim-{uuid.uuid4().hex}"
    seed_pages(factory, prefix, 100, distinct_hosts=True)
    token = prefix.rsplit("/", 1)[-1]
    claim_urls = tuple(
        f"https://p3-{token}-{index}.nptu.edu.tw/{index}" for index in range(100)
    )
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
                urls=claim_urls,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = [
                claim_result
                for result in pool.map(claim, range(4))
                for claim_result in result
            ]

        retry_worker = 0
        while len(claims) < 100 and retry_worker < 8:
            claims.extend(
                repository.claim_due(
                    owner=f"p3-retry-worker-{retry_worker}",
                    limit=25,
                    lease_duration=timedelta(minutes=5),
                    urls=claim_urls,
                )
            )
            retry_worker += 1

        assert len(claims) == 100
        assert len({claim.page_id for claim in claims}) == 100
        assert len({claim.token for claim in claims}) == 100
        assert (
            sum("UPDATE SITE_PAGES" in statement.upper() for statement in statements)
            >= 4
        )
        assert len(statements) <= 30
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SiteCrawlAttempt)
                    .where(
                        SiteCrawlAttempt.site_page_id.in_(
                            tuple(claim.page_id for claim in claims)
                        )
                    )
                )
                == 100
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        cleanup(factory, prefix, distinct_hosts=True)
        engine.dispose()


def test_postgres_same_host_claim_is_one_active_and_recovers_expired_lease() -> None:
    factory, engine = make_factory()
    prefix = f"https://www.nptu.edu.tw/p3-host-cap-{uuid.uuid4().hex}"
    seed_pages(factory, prefix, 3)
    claim_urls = tuple(f"{prefix}/{index}" for index in range(3))
    repository = SqlCrawlSchedulerRepository(factory)
    now = datetime.now(timezone.utc)
    try:

        def claim(owner: str):
            return repository.claim_due(
                owner=owner,
                limit=3,
                lease_duration=timedelta(seconds=1),
                now=now,
                urls=claim_urls,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(claim, ("p3-host-worker-a", "p3-host-worker-b"))
        assert len(first) + len(second) == 1
        claimed = first[0] if first else second[0]
        recovery_owner = "p3-host-worker-b" if first else "p3-host-worker-a"

        recovered = repository.claim_due(
            owner=recovery_owner,
            limit=3,
            lease_duration=timedelta(minutes=5),
            now=now + timedelta(seconds=2),
            urls=claim_urls,
        )
        assert len(recovered) == 1
        assert recovered[0].page_id == claimed.page_id
        with factory() as session:
            active = session.scalar(
                select(func.count())
                .select_from(SitePage)
                .where(
                    SitePage.host == f"p3-{prefix.rsplit('/', 1)[-1]}.nptu.edu.tw",
                    SitePage.crawl_lease_expires_at > now + timedelta(seconds=2),
                )
            )
            assert active == 1
    finally:
        cleanup(factory, prefix)
        engine.dispose()


def test_expired_lease_fences_stale_completion_and_records_attempt() -> None:
    factory, engine = make_factory()
    prefix = f"https://www.nptu.edu.tw/p3-expiry-{uuid.uuid4().hex}"
    seed_pages(factory, prefix, 1)
    claim_urls = (f"{prefix}/0",)
    now = datetime.now(timezone.utc)
    repository = SqlCrawlSchedulerRepository(factory)
    try:
        first = repository.claim_due(
            owner="p3-worker-a",
            limit=1,
            lease_duration=timedelta(seconds=1),
            now=now,
            urls=claim_urls,
        )[0]
        second = repository.claim_due(
            owner="p3-worker-b",
            limit=1,
            lease_duration=timedelta(minutes=5),
            now=now + timedelta(seconds=2),
            urls=claim_urls,
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
            assert Counter(attempt_outcomes)["lease_lost"] == 1
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
            "ingestion_content_hash",
            "ingestion_attempt_hash",
            "ingestion_status",
            "ingestion_error",
            "announcement_ingestion_status",
            "ingestion_lease_token",
            "ingestion_lease_owner",
            "ingestion_lease_expires_at",
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
            "final_url",
        } <= attempt_columns
        index_names = {
            item["name"] for item in inspect(engine).get_indexes("site_pages")
        }
        assert {
            "ix_site_pages_crawl_schedule",
            "ix_site_pages_host_next_crawl_at",
            "ix_site_pages_host_crawl_lease_expires_at",
            "ix_site_pages_crawl_lease_expires_at",
            "ix_site_pages_due_active_crawlable",
            "ix_site_pages_ingestion_status",
            "ix_site_pages_ingestion_lease_expires_at",
        } <= index_names
        checks = {
            check["name"]
            for check in inspect(engine).get_check_constraints("site_pages")
        }
        assert {
            "ck_site_pages_ingestion_status",
            "ck_site_pages_announcement_ingestion_status",
        } <= checks
    finally:
        engine.dispose()


def test_postgres_migration_roundtrip_preserves_ingestion_semantics() -> None:
    """Exercise 0009 data backfill, downgrade compatibility, and restoration."""

    factory, engine = make_factory()
    token = uuid.uuid4().hex
    prefix = f"https://p31-migration-{token}.nptu.edu.tw"
    now = datetime.now(timezone.utc)
    hashes = {
        "success": "1" * 64,
        "failed": "2" * 64,
        "pending": "3" * 64,
        "partial": "4" * 64,
        "incomplete": "5" * 64,
    }
    rows = [
        SitePage(
            canonical_url=f"{prefix}/{name}",
            host=f"p31-migration-{token}.nptu.edu.tw",
            path=f"/{name}",
            page_type=(
                SitePageType.ANNOUNCEMENT_LISTING.value
                if name == "incomplete"
                else SitePageType.GENERAL_PAGE.value
            ),
            crawl_status=SiteCrawlStatus.SUCCESS.value,
            next_crawl_at=now + timedelta(hours=1),
            content_hash=hashes[name],
            ingestion_content_hash=hashes[name],
            ingestion_status=("partial" if name == "incomplete" else name),
            ingestion_error=("retryable" if name == "failed" else None),
            announcement_ingestion_status=(
                "incomplete" if name == "incomplete" else "not_applicable"
            ),
            announcement_ingestion_error=(
                "官方頁面未提供日期" if name == "incomplete" else None
            ),
            is_active=True,
            is_indexable=True,
        )
        for name in ("success", "failed", "pending", "partial", "incomplete")
    ]
    # The fixture mirrors 0008's historical meaning for pending/failed: the
    # attempted fetched hash was stored in ingestion_content_hash.
    rows[1].ingestion_status = "failed"
    rows[2].ingestion_status = "pending"
    rows[3].ingestion_status = "partial"
    rows[3].announcement_ingestion_status = "failed"
    rows[3].announcement_ingestion_error = "announcement retryable"

    with factory.begin() as session:
        session.add_all(rows)

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        engine.dispose()
        command.downgrade(config, "20260803_0008")
        assert "ingestion_attempt_hash" not in {
            column["name"] for column in inspect(engine).get_columns("site_pages")
        }
        with factory() as session:
            downgraded = {
                row["canonical_url"].rsplit("/", 1)[-1]: row
                for row in session.execute(
                    text(
                        "SELECT canonical_url, ingestion_content_hash, "
                        "announcement_ingestion_status, announcement_ingestion_error "
                        "FROM site_pages WHERE canonical_url LIKE :prefix"
                    ),
                    {"prefix": f"{prefix}/%"},
                ).mappings()
            }
            assert downgraded["failed"]["ingestion_content_hash"] == hashes["failed"]
            assert downgraded["pending"]["ingestion_content_hash"] == hashes["pending"]
            assert downgraded["partial"]["announcement_ingestion_status"] == "failed"
            assert (
                downgraded["incomplete"]["announcement_ingestion_status"] == "pending"
            )
            assert downgraded["incomplete"]["announcement_ingestion_error"].startswith(
                "terminal_incomplete:"
            )

        command.upgrade(config, "head")
        assert "ingestion_attempt_hash" in {
            column["name"] for column in inspect(engine).get_columns("site_pages")
        }
        with factory() as session:
            upgraded = {
                page.canonical_url.rsplit("/", 1)[-1]: page
                for page in session.scalars(
                    select(SitePage).where(SitePage.canonical_url.like(f"{prefix}/%"))
                )
            }
            assert upgraded["success"].ingestion_content_hash == hashes["success"]
            assert upgraded["success"].ingestion_attempt_hash is None
            for name in ("failed", "pending"):
                assert upgraded[name].ingestion_content_hash is None
                assert upgraded[name].ingestion_attempt_hash == hashes[name]
            assert upgraded["partial"].ingestion_content_hash == hashes["partial"]
            assert upgraded["partial"].announcement_ingestion_status == "failed"
            assert upgraded["incomplete"].ingestion_content_hash == hashes["incomplete"]
            assert upgraded["incomplete"].announcement_ingestion_status == "incomplete"
            assert upgraded["incomplete"].announcement_ingestion_error == (
                "官方頁面未提供日期"
            )
    finally:
        # Leave the shared CI database at head even if an assertion fails.
        command.upgrade(config, "head")
        cleanup(factory, prefix)
        engine.dispose()
