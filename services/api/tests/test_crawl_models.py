from __future__ import annotations

from pathlib import Path

from nptu_assistant.db.crawl_models import SiteCrawlAttempt
from nptu_assistant.db.models import SitePage


def test_site_crawl_models_expose_lease_and_attempt_metadata() -> None:
    assert {
        "crawl_lease_token",
        "crawl_lease_owner",
        "crawl_lease_expires_at",
        "ingestion_content_hash",
        "ingestion_attempt_hash",
        "ingestion_status",
        "ingestion_lease_token",
        "ingestion_lease_owner",
        "ingestion_lease_expires_at",
    } <= set(SitePage.__table__.c.keys())
    assert {
        "site_page_id",
        "lease_token",
        "started_at",
        "finished_at",
        "outcome",
        "http_status",
        "content_type",
        "content_length",
        "etag",
        "last_modified",
        "error_code",
        "error_message",
    } <= set(SiteCrawlAttempt.__table__.c.keys())
    assert "ix_site_pages_crawl_lease_expires_at" in {
        index.name for index in SitePage.__table__.indexes
    }
    assert {
        "ix_site_crawl_attempts_page_started_at",
        "ix_site_crawl_attempts_outcome",
        "ix_site_crawl_attempts_lease_token",
    } <= {index.name for index in SiteCrawlAttempt.__table__.indexes}


def test_crawl_migration_round_trip_has_current_parent_and_reversible_objects() -> None:
    migration = (
        Path(__file__)
        .resolve()
        .parents[3]
        .joinpath("database/migrations/versions/20260802_0007_crawl_attempts.py")
    )
    source = migration.read_text(encoding="utf-8")

    assert 'revision: str = "20260802_0007"' in source
    assert 'down_revision: str | None = "20260723_0006"' in source
    assert 'op.create_table(\n        "site_crawl_attempts"' in source
    assert 'op.drop_table("site_crawl_attempts")' in source
    assert 'op.drop_column("site_pages", "crawl_lease_token")' in source


def test_ingestion_recovery_migration_is_append_only_and_reversible() -> None:
    migration = (
        Path(__file__)
        .resolve()
        .parents[3]
        .joinpath("database/migrations/versions/20260803_0008_ingestion_recovery.py")
    )
    source = migration.read_text(encoding="utf-8")

    assert 'revision: str = "20260803_0008"' in source
    assert 'down_revision: str | None = "20260802_0007"' in source
    assert 'op.add_column("site_pages", sa.Column("ingestion_content_hash"' in source
    assert 'op.drop_column("site_pages", "ingestion_content_hash")' in source
