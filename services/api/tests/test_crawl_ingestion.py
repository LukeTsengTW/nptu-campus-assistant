from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePage
from nptu_assistant.crawlers.crawl_ingestion import (
    CrawlIngestionService,
    CrawlIngestionStatus,
)
from nptu_assistant.db.base import Base
from nptu_assistant.db.models import SitePage
from nptu_assistant.db.repositories import SqlDocumentRepository
from nptu_assistant.ingestion.chunking import chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata


@dataclass
class SavedDocument:
    canonical_url: str
    raw_text: str
    is_current: bool = True


class MemoryDocumentRepository:
    def __init__(self) -> None:
        self.documents: list[SavedDocument] = []
        self.fail_urls: set[str] = set()

    def has_hash(self, canonical_url: str, digest: str) -> bool:
        return any(
            document.canonical_url == canonical_url
            and content_hash(document.raw_text) == digest
            for document in self.documents
        )

    def save(self, metadata, raw_text, chunks, embeddings) -> None:
        assert len(chunks) == len(embeddings)
        if str(metadata.source_url) in self.fail_urls:
            raise RuntimeError("document transaction failed")
        for document in self.documents:
            if document.canonical_url == str(metadata.source_url):
                document.is_current = False
        self.documents.append(SavedDocument(str(metadata.source_url), raw_text))


class RecordingEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str], *, timeout_seconds=None) -> list[list[float]]:
        del timeout_seconds
        self.calls.append(texts)
        return [[0.0] * 1536 for _ in texts]


def page(body: str, url: str = "https://www.nptu.edu.tw/rules") -> NptuSitePage:
    return NptuSitePage(
        title="校務規章",
        canonical_url=url,
        body=body,
        published_at=date(2026, 8, 1),
        links=(),
    )


def test_crawl_ingestion_only_embeds_changed_content() -> None:
    repository = MemoryDocumentRepository()
    embeddings = RecordingEmbeddingProvider()
    service = CrawlIngestionService(repository, embeddings, default_unit="教務處")

    first = service.ingest_page(page("第一版內容"), allow_unleased=True)
    unchanged = service.ingest_page(page("第一版內容"), allow_unleased=True)
    changed = service.ingest_page(page("第二版內容"), allow_unleased=True)

    assert first.status is CrawlIngestionStatus.CREATED
    assert unchanged.status is CrawlIngestionStatus.SKIPPED
    assert changed.status is CrawlIngestionStatus.CREATED
    assert len(embeddings.calls) == 2
    assert [document.is_current for document in repository.documents] == [
        False,
        True,
    ]


def test_crawl_ingestion_deduplicates_same_page_in_one_batch() -> None:
    repository = MemoryDocumentRepository()
    embeddings = RecordingEmbeddingProvider()
    service = CrawlIngestionService(repository, embeddings, default_unit="教務處")

    summary = service.ingest_pages(
        [page("相同內容"), page("相同內容")], allow_unleased=True
    )

    assert summary.created == 1
    assert summary.skipped == 1
    assert summary.failed == 0
    assert len(repository.documents) == 1
    assert len(embeddings.calls) == 1


def test_crawl_ingestion_failure_keeps_last_success_and_continues_batch() -> None:
    repository = MemoryDocumentRepository()
    embeddings = RecordingEmbeddingProvider()
    service = CrawlIngestionService(repository, embeddings, default_unit="教務處")
    service.ingest_page(page("最後成功版本"), allow_unleased=True)

    failed_page = page("無法保存的新版本")
    other_page = page("另一頁內容", "https://www.nptu.edu.tw/other")
    repository.fail_urls.add(failed_page.canonical_url)
    summary = service.ingest_pages([failed_page, other_page], allow_unleased=True)

    assert summary.created == 1
    assert summary.failed == 1
    assert len(repository.documents) == 2
    assert repository.documents[0].raw_text == "最後成功版本"
    assert repository.documents[0].is_current is True
    assert failed_page.canonical_url in summary.errors[0]


def _sqlite_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class FailingEmbeddingProvider:
    def embed(self, texts: list[str], *, timeout_seconds=None) -> list[list[float]]:
        del texts, timeout_seconds
        raise RuntimeError("embedding temporarily unavailable")


def _leased_site_page(
    factory: sessionmaker[Session],
    *,
    page_id,
    owner: str,
    token,
    expires_at: datetime,
) -> None:
    with factory.begin() as session:
        session.add(
            SitePage(
                id=page_id,
                canonical_url="https://www.nptu.edu.tw/rules",
                host="www.nptu.edu.tw",
                path="/rules",
                crawl_lease_owner=owner,
                crawl_lease_token=token,
                crawl_lease_expires_at=expires_at,
            )
        )


def test_sqlite_pending_failed_success_recovery_is_fenced() -> None:
    factory = _sqlite_factory()
    repository = SqlDocumentRepository(factory)
    page_id = uuid4()
    first_token = uuid4()
    now = datetime.now(timezone.utc)
    _leased_site_page(
        factory,
        page_id=page_id,
        owner="worker-a",
        token=first_token,
        expires_at=now + timedelta(minutes=5),
    )

    failed = CrawlIngestionService(
        repository,
        FailingEmbeddingProvider(),
        default_unit="教務處",
    ).ingest_page(
        page("待恢復內容"),
        page_id=page_id,
        lease_owner="worker-a",
        lease_token=first_token,
        lease_expires_at=now + timedelta(minutes=5),
    )
    assert failed.status is CrawlIngestionStatus.FAILED

    with factory() as session:
        state = session.scalar(select(SitePage).where(SitePage.id == page_id))
        assert state is not None
        assert state.ingestion_status == "failed"
        assert state.ingestion_content_hash == content_hash("待恢復內容")

    second_token = uuid4()
    with factory.begin() as session:
        state = session.scalar(select(SitePage).where(SitePage.id == page_id))
        assert state is not None
        state.crawl_lease_owner = "worker-b"
        state.crawl_lease_token = second_token
        state.crawl_lease_expires_at = now + timedelta(minutes=5)

    recovered = CrawlIngestionService(
        repository,
        RecordingEmbeddingProvider(),
        default_unit="教務處",
    ).ingest_page(
        page("待恢復內容"),
        page_id=page_id,
        lease_owner="worker-b",
        lease_token=second_token,
        lease_expires_at=now + timedelta(minutes=5),
    )
    assert recovered.status is CrawlIngestionStatus.CREATED
    with factory() as session:
        state = session.scalar(select(SitePage).where(SitePage.id == page_id))
        assert state is not None
        assert state.ingestion_status == "success"


def test_sqlite_document_save_is_idempotent_across_repository_instances() -> None:
    factory = _sqlite_factory()
    first = SqlDocumentRepository(factory)
    second = SqlDocumentRepository(factory)
    metadata = DocumentMetadata(
        title="規章",
        source_url="https://www.nptu.edu.tw/idempotent",
        unit="教務處",
        published_at=date(2026, 8, 1),
        effective_from=date(2026, 8, 1),
        document_type="official_web_page",
        version="v1",
    )
    chunks = chunk_text("同一份內容")
    embeddings = [[0.0] * 1536 for _ in chunks]

    assert first.save_idempotent(metadata, "同一份內容", chunks, embeddings) is True
    assert second.save_idempotent(metadata, "同一份內容", chunks, embeddings) is False
