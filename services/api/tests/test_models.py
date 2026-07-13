from pathlib import Path

from nptu_assistant.db.models import Announcement, Document, DocumentChunk, Source


def test_required_tables_and_vector_dimension() -> None:
    assert Source.__tablename__ == "sources"
    assert Document.__tablename__ == "documents"
    assert DocumentChunk.__tablename__ == "document_chunks"
    assert Announcement.__tablename__ == "announcements"
    assert DocumentChunk.__table__.c.embedding.type.dim == 1536
    assert Announcement.__table__.c.warning.nullable is True
    assert Source.__table__.c.canonical_urls.nullable is False
    assert Source.__table__.c.last_successful_crawl_at.nullable is True


def test_snapshot_migration_does_not_invent_a_successful_legacy_crawl() -> None:
    workspace_root = Path(__file__).resolve().parents[3]
    migration = workspace_root.joinpath(
        "database/migrations/versions/20260713_0003_source_refresh_snapshots.py"
    ).read_text(encoding="utf-8")

    assert "last_successful_crawl_at" in migration
    assert "UPDATE sources" not in migration
