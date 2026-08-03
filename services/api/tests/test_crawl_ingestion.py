from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuListingItem,
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.announcement_adapter import (
    AnnouncementSourceIdentity,
    IncrementalAnnouncementAdapter,
)
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


ANNOUNCEMENT_URL = "https://www.nptu.edu.tw/p/406-1025-197412.php"
ANNOUNCEMENT_SOURCE = AnnouncementSourceIdentity(
    name="教務處公告",
    url="https://www.nptu.edu.tw/p/403-1025-1019.php",
    unit="教務處",
)


class MemoryAnnouncementIngestionRepository(MemoryDocumentRepository):
    """保留 page ingestion 與公告 terminal 狀態的測試 repository。"""

    def __init__(self, *, fail_announcement: bool = False) -> None:
        super().__init__()
        self.fail_announcement = fail_announcement
        self.page_states: dict[str, dict[str, str | None]] = {}
        self.announcement_items: dict[str, object] = {}
        self.merge_calls = 0

    def save_idempotent(
        self,
        metadata,
        raw_text,
        chunks,
        embeddings,
        **kwargs,
    ) -> bool:
        del kwargs
        self.save(metadata, raw_text, chunks, embeddings)
        return True

    def begin_ingestion(self, canonical_url, digest, *, page_id, **kwargs) -> str:
        del kwargs
        state = self.page_states.setdefault(
            str(page_id),
            {
                "canonical_url": canonical_url,
                "hash": None,
                "ingestion": "pending",
                "announcement": "not_applicable",
            },
        )
        if state["hash"] == digest and state["announcement"] in {
            "success",
            "incomplete",
        }:
            return "success"
        state.update(
            hash=digest,
            ingestion="pending",
            announcement="pending",
        )
        return "pending"

    def complete_ingestion(self, canonical_url, digest, *, page_id, **kwargs) -> bool:
        del canonical_url, digest, kwargs
        self.page_states[str(page_id)]["ingestion"] = "success"
        return True

    def complete_announcement_ingestion(
        self,
        canonical_url,
        digest,
        *,
        page_id,
        status="success",
        **kwargs,
    ) -> bool:
        del canonical_url, digest, kwargs
        state = self.page_states[str(page_id)]
        state["announcement"] = status
        state["ingestion"] = "partial" if status == "incomplete" else "success"
        return True

    def fail_announcement_ingestion(
        self,
        canonical_url,
        digest,
        *,
        page_id,
        error=None,
        **kwargs,
    ) -> bool:
        del canonical_url, digest, error, kwargs
        state = self.page_states[str(page_id)]
        state["announcement"] = "failed"
        state["ingestion"] = "partial"
        return True

    def fail_ingestion(self, canonical_url, digest, *, page_id, **kwargs) -> bool:
        del canonical_url, digest, kwargs
        self.page_states[str(page_id)]["ingestion"] = "failed"
        return True

    def merge_source_announcements(
        self,
        candidates,
        *,
        source_name,
        source_url,
        source_unit,
        interval_minutes,
        crawled_at,
    ) -> list[str]:
        del source_name, source_url, source_unit, interval_minutes, crawled_at
        self.merge_calls += 1
        if self.fail_announcement:
            raise RuntimeError("公告資料庫暫時不可用")
        for candidate in candidates:
            self.announcement_items[candidate.canonical_url] = candidate
        return ["created" for _ in candidates]


def announcement_listing_page(*, body: str = "公告列表") -> NptuSitePage:
    return NptuSitePage(
        title="公告列表",
        canonical_url=ANNOUNCEMENT_URL,
        body=body,
        published_at=None,
        links=(ANNOUNCEMENT_URL,),
        role=UnitAnnouncementPageRole.LISTING,
        announcement_items=(
            NptuListingItem(
                title="待補日期公告",
                canonical_url=ANNOUNCEMENT_URL,
                published_at=None,
                summary="列表摘要",
                anchor_text="待補日期公告",
                order=0,
            ),
        ),
    )


def announcement_detail_page(
    *,
    body: str = "公告完整正文",
    published_at: date | None = date(2026, 8, 2),
) -> NptuSitePage:
    return NptuSitePage(
        title="待補日期公告完整標題",
        canonical_url=ANNOUNCEMENT_URL,
        body=body,
        published_at=published_at,
        links=(),
        role=UnitAnnouncementPageRole.DETAIL,
    )


def _announcement_service(repository, embeddings):
    return CrawlIngestionService(
        repository,
        embeddings,
        default_unit="教務處",
        announcement_adapter=IncrementalAnnouncementAdapter(repository),
        announcement_source_resolver=lambda _page, _unit: ANNOUNCEMENT_SOURCE,
    )


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
        assert state.ingestion_content_hash != content_hash("待恢復內容")
        assert state.ingestion_attempt_hash == content_hash("待恢復內容")

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


def test_undated_announcement_is_terminal_incomplete_and_same_hash_skips() -> None:
    repository = MemoryAnnouncementIngestionRepository()
    embeddings = RecordingEmbeddingProvider()
    service = _announcement_service(repository, embeddings)
    page_id = uuid4()
    lease = {"page_id": page_id, "lease_owner": "worker-a", "lease_token": uuid4()}

    first = service.ingest_page(
        announcement_listing_page(),
        **lease,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    second = service.ingest_page(
        announcement_listing_page(),
        **lease,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    state = repository.page_states[str(page_id)]
    assert first.status is CrawlIngestionStatus.INCOMPLETE
    assert first.announcement_incomplete == 1
    assert first.announcement_failed == 0
    assert state["announcement"] == "incomplete"
    assert state["ingestion"] == "partial"
    assert second.status is CrawlIngestionStatus.SKIPPED
    assert len(embeddings.calls) == 1


def test_mixed_listing_persists_dated_item_and_marks_page_incomplete() -> None:
    repository = MemoryAnnouncementIngestionRepository()
    embeddings = RecordingEmbeddingProvider()
    service = _announcement_service(repository, embeddings)
    page_id = uuid4()
    lease = {"page_id": page_id, "lease_owner": "worker-a", "lease_token": uuid4()}
    dated_url = "https://www.nptu.edu.tw/p/406-1025-197413.php"
    undated_url = "https://www.nptu.edu.tw/p/406-1025-197414.php"
    listing = NptuSitePage(
        title="公告列表",
        canonical_url="https://www.nptu.edu.tw/p/403-1025-1019.php",
        body="公告列表",
        published_at=None,
        links=(dated_url, undated_url),
        role=UnitAnnouncementPageRole.LISTING,
        announcement_items=(
            NptuListingItem(
                title="已有日期公告",
                canonical_url=dated_url,
                published_at=date(2026, 8, 1),
                summary="已有日期摘要",
                anchor_text="已有日期公告",
                order=0,
            ),
            NptuListingItem(
                title="尚無日期公告",
                canonical_url=undated_url,
                published_at=None,
                summary="尚無日期摘要",
                anchor_text="尚無日期公告",
                order=1,
            ),
        ),
    )

    result = service.ingest_page(
        listing,
        **lease,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    state = repository.page_states[str(page_id)]
    dated_candidate = repository.announcement_items[dated_url]
    assert result.status is CrawlIngestionStatus.INCOMPLETE
    assert result.announcement_persisted == 1
    assert result.announcement_incomplete == 1
    assert result.announcement_failed == 0
    assert dated_candidate.published_at == date(2026, 8, 1)
    assert undated_url not in repository.announcement_items
    assert state["announcement"] == "incomplete"


def test_detail_date_can_reingest_incomplete_announcement_to_success() -> None:
    repository = MemoryAnnouncementIngestionRepository()
    embeddings = RecordingEmbeddingProvider()
    service = _announcement_service(repository, embeddings)
    page_id = uuid4()
    lease = {"page_id": page_id, "lease_owner": "worker-a", "lease_token": uuid4()}
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    first = service.ingest_page(
        announcement_listing_page(),
        **lease,
        lease_expires_at=expires_at,
    )
    enriched = service.ingest_page(
        announcement_detail_page(),
        **lease,
        lease_expires_at=expires_at,
    )

    state = repository.page_states[str(page_id)]
    candidate = repository.announcement_items[ANNOUNCEMENT_URL]
    assert first.status is CrawlIngestionStatus.INCOMPLETE
    assert enriched.status is CrawlIngestionStatus.CREATED
    assert enriched.announcement_incomplete == 0
    assert enriched.announcement_failed == 0
    assert state["announcement"] == "success"
    assert state["ingestion"] == "success"
    assert candidate.published_at == date(2026, 8, 2)


def test_announcement_repository_failure_remains_failed_and_retryable() -> None:
    repository = MemoryAnnouncementIngestionRepository(fail_announcement=True)
    embeddings = RecordingEmbeddingProvider()
    service = _announcement_service(repository, embeddings)
    page_id = uuid4()
    lease = {"page_id": page_id, "lease_owner": "worker-a", "lease_token": uuid4()}
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    first = service.ingest_page(
        announcement_detail_page(),
        **lease,
        lease_expires_at=expires_at,
    )
    second = service.ingest_page(
        announcement_detail_page(),
        **lease,
        lease_expires_at=expires_at,
    )

    state = repository.page_states[str(page_id)]
    assert first.status is CrawlIngestionStatus.PARTIAL
    assert second.status is CrawlIngestionStatus.PARTIAL
    assert first.announcement_failed == 1
    assert first.announcement_incomplete == 0
    assert state["announcement"] == "failed"
    assert repository.merge_calls == 2
    assert len(embeddings.calls) == 2


def test_incomplete_status_migration_is_added_after_0008() -> None:
    migration_dir = Path(__file__).parents[3] / "database" / "migrations" / "versions"
    migration = migration_dir / "20260803_0009_announcement_incomplete.py"
    previous = migration_dir / "20260803_0008_ingestion_recovery.py"

    assert migration.exists()
    content = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "20260803_0008"' in content
    assert "'incomplete'" in content
    assert "ck_site_pages_announcement_ingestion_status" in content
    assert "incomplete" not in previous.read_text(encoding="utf-8")
