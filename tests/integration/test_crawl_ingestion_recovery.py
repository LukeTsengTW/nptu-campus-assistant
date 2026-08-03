from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from nptu_assistant.db.models import Document, DocumentChunk
from nptu_assistant.db.repositories import SqlDocumentRepository
from nptu_assistant.db.repositories import SqlAnnouncementRepository
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.ingestion.chunking import chunk_text
from nptu_assistant.ingestion.metadata import DocumentMetadata


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="需要已套用 migration 的 PostgreSQL/pgvector 資料庫",
)


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
