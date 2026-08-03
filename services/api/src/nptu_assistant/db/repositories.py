from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from uuid import UUID
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from nptu_assistant.api.schemas import AnnouncementItem, AnnouncementListResponse
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.db.models import (
    Announcement,
    Document,
    DocumentChunk,
    SitePage,
    Source,
)
from nptu_assistant.ingestion.chunking import TextChunk
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata


logger = logging.getLogger(__name__)


class SourceRefreshLease:
    """A PostgreSQL session advisory lock held across one source HTTP crawl."""

    def __init__(self, session: Session, key: str) -> None:
        self._session = session
        self._key = key
        self._released = False

    def __enter__(self) -> SourceRefreshLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._session.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": self._key},
            )
        except Exception:
            logger.exception("來源 refresh advisory lock 釋放失敗")
        finally:
            self._session.close()


class _NoopSourceRefreshLease:
    def __enter__(self) -> _NoopSourceRefreshLease:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _base_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def get_or_create_source(
    session: Session,
    *,
    name: str,
    base_url: str,
    unit: str,
    source_type: str,
    crawl_enabled: bool = False,
    crawl_interval_minutes: int = 60,
) -> Source:
    if session.get_bind().dialect.name == "postgresql":
        # Listing/detail workers share source identity. Serialize the
        # SELECT/INSERT pair so a first-use source cannot race across workers.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"nptu-source:{name}"},
        )
    source = session.scalar(select(Source).where(Source.name == name).with_for_update())
    if source:
        source.base_url = base_url
        source.unit = unit
        source.source_type = source_type
        source.crawl_enabled = crawl_enabled
        source.crawl_interval_minutes = crawl_interval_minutes
        return source
    source = Source(
        name=name,
        base_url=base_url,
        unit=unit,
        source_type=source_type,
        crawl_enabled=crawl_enabled,
        crawl_interval_minutes=crawl_interval_minutes,
    )
    session.add(source)
    session.flush()
    return source


class SqlDocumentRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def has_hash(self, canonical_url: str, digest: str) -> bool:
        with self._factory() as session:
            return (
                session.scalar(
                    select(Document.id).where(
                        Document.canonical_url == canonical_url,
                        Document.content_hash == digest,
                    )
                )
                is not None
            )

    def needs_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str | None = None,
        lease_owner: str | None = None,
        lease_token: UUID | str | None = None,
        lease_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        del lease_expires_at
        if page_id is None:
            return not self.has_hash(canonical_url, digest)
        with self._factory.begin() as session:
            page = self._locked_ingestion_page(
                session,
                page_id=page_id,
                canonical_url=canonical_url,
            )
            self._assert_crawl_lease(
                page,
                owner=lease_owner,
                token=lease_token,
                now=now or datetime.now(timezone.utc),
            )
            is_announcement = page.page_type in {
                "announcement_listing",
                "announcement_detail",
            }
            if is_announcement:
                if page.ingestion_status not in {"success", "partial"}:
                    return True
                if page.announcement_ingestion_status not in {
                    "success",
                    "incomplete",
                }:
                    return True
                if page.ingestion_content_hash != digest:
                    return True
            elif page.ingestion_status != "success":
                return True
            return (
                session.scalar(
                    select(Document.id).where(
                        Document.canonical_url == canonical_url,
                        Document.content_hash == digest,
                    )
                )
                is None
            )

    def begin_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str,
        lease_owner: str,
        lease_token: UUID | str,
        lease_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> str:
        del lease_expires_at
        checked_at = now or datetime.now(timezone.utc)
        with self._factory.begin() as session:
            page = self._locked_ingestion_page(
                session,
                page_id=page_id,
                canonical_url=canonical_url,
            )
            self._assert_crawl_lease(
                page,
                owner=lease_owner,
                token=lease_token,
                now=checked_at,
            )
            existing = session.scalar(
                select(Document.id).where(
                    Document.canonical_url == canonical_url,
                    Document.content_hash == digest,
                )
            )
            if existing is not None:
                is_announcement = page.page_type in {
                    "announcement_listing",
                    "announcement_detail",
                }
                if is_announcement:
                    if (
                        page.ingestion_status in {"success", "partial"}
                        and page.ingestion_content_hash == digest
                        and page.announcement_ingestion_status
                        in {"success", "incomplete"}
                    ):
                        return "success"
                    page.ingestion_attempt_hash = digest
                    page.ingestion_status = "pending"
                    page.ingestion_error = None
                    page.announcement_ingestion_status = "pending"
                    page.announcement_ingestion_error = None
                    page.ingestion_lease_owner = lease_owner
                    page.ingestion_lease_token = self._as_uuid(lease_token)
                    page.ingestion_lease_expires_at = page.crawl_lease_expires_at
                    return "pending"
                self._mark_ingestion_success(page, digest)
                return "success"
            page.ingestion_attempt_hash = digest
            page.ingestion_status = "pending"
            page.ingestion_error = None
            if page.page_type in {"announcement_listing", "announcement_detail"}:
                page.announcement_ingestion_status = "pending"
                page.announcement_ingestion_error = None
            page.ingestion_lease_owner = lease_owner
            page.ingestion_lease_token = self._as_uuid(lease_token)
            page.ingestion_lease_expires_at = page.crawl_lease_expires_at
            return "pending"

    def complete_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str,
        lease_owner: str,
        lease_token: UUID | str,
        lease_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        del lease_expires_at
        checked_at = now or datetime.now(timezone.utc)
        with self._factory.begin() as session:
            page = self._locked_ingestion_page(
                session,
                page_id=page_id,
                canonical_url=canonical_url,
            )
            self._assert_ingestion_lease(
                page,
                digest=digest,
                owner=lease_owner,
                token=lease_token,
                now=checked_at,
            )
            if page.page_type in {"announcement_listing", "announcement_detail"}:
                page.ingestion_status = "success"
                page.ingestion_content_hash = digest
                page.ingestion_error = None
                if page.announcement_ingestion_status == "not_applicable":
                    page.announcement_ingestion_status = "pending"
            else:
                self._mark_ingestion_success(page, digest)
            return True

    def complete_announcement_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str,
        lease_owner: str,
        lease_token: UUID | str,
        status: str = "success",
        warning: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if status not in {"success", "incomplete"}:
            raise ValueError("公告 ingestion terminal status 不合法")
        checked_at = now or datetime.now(timezone.utc)
        with self._factory.begin() as session:
            page = self._locked_ingestion_page(
                session,
                page_id=page_id,
                canonical_url=canonical_url,
            )
            self._assert_ingestion_lease(
                page,
                digest=digest,
                owner=lease_owner,
                token=lease_token,
                now=checked_at,
            )
            page.announcement_ingestion_status = status
            page.announcement_ingestion_error = (
                (warning or "公告項目缺少官方日期，標記為不完整")[:1000]
                if status == "incomplete"
                else None
            )
            page.ingestion_status = "partial" if status == "incomplete" else "success"
            page.ingestion_error = page.announcement_ingestion_error
            page.ingestion_attempt_hash = None
            self._clear_ingestion_lease(page)
            return True

    def fail_announcement_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str,
        lease_owner: str,
        lease_token: UUID | str,
        error: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        checked_at = now or datetime.now(timezone.utc)
        with self._factory.begin() as session:
            page = self._locked_ingestion_page(
                session,
                page_id=page_id,
                canonical_url=canonical_url,
            )
            self._assert_ingestion_lease(
                page,
                digest=digest,
                owner=lease_owner,
                token=lease_token,
                now=checked_at,
            )
            page.ingestion_status = "partial"
            page.announcement_ingestion_status = "failed"
            page.announcement_ingestion_error = (error or "公告 ingestion 失敗")[:1000]
            self._clear_ingestion_lease(page)
            return True

    def fail_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str,
        lease_owner: str,
        lease_token: UUID | str,
        lease_expires_at: datetime | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        del lease_expires_at
        checked_at = now or datetime.now(timezone.utc)
        with self._factory.begin() as session:
            page = self._locked_ingestion_page(
                session,
                page_id=page_id,
                canonical_url=canonical_url,
            )
            self._assert_ingestion_lease(
                page,
                digest=digest,
                owner=lease_owner,
                token=lease_token,
                now=checked_at,
            )
            page.ingestion_status = "failed"
            page.ingestion_attempt_hash = digest
            page.ingestion_error = (error or "未知錯誤")[:1000]
            self._clear_ingestion_lease(page)
            return True

    @staticmethod
    def _locked_ingestion_page(
        session: Session,
        *,
        page_id: UUID | str,
        canonical_url: str,
    ) -> SitePage:
        page = session.scalar(
            select(SitePage)
            .where(
                SitePage.id == SqlDocumentRepository._as_uuid(page_id),
                SitePage.canonical_url == canonical_url,
            )
            .with_for_update()
        )
        if page is None:
            raise RuntimeError("找不到 ingestion page")
        return page

    @staticmethod
    def _assert_crawl_lease(
        page: SitePage,
        *,
        owner: str | None,
        token: UUID | str | None,
        now: datetime,
    ) -> None:
        expires_at = SqlDocumentRepository._as_utc(page.crawl_lease_expires_at)
        if expires_at is None or expires_at <= now:
            raise RuntimeError("page lease 已失效，拒絕 ingestion 狀態變更")
        if not owner or token is None:
            raise RuntimeError("page lease context 不完整")
        if (
            page.crawl_lease_owner != owner
            or page.crawl_lease_token != SqlDocumentRepository._as_uuid(token)
        ):
            raise RuntimeError("page lease 已失效，拒絕 ingestion 狀態變更")

    @classmethod
    def _assert_ingestion_lease(
        cls,
        page: SitePage,
        *,
        digest: str,
        owner: str,
        token: UUID | str,
        now: datetime,
    ) -> None:
        cls._assert_crawl_lease(page, owner=owner, token=token, now=now)
        ingestion_expires_at = SqlDocumentRepository._as_utc(
            page.ingestion_lease_expires_at
        )
        if ingestion_expires_at is None or ingestion_expires_at <= now:
            raise RuntimeError("ingestion lease 已失效，拒絕狀態變更")
        if (
            page.ingestion_attempt_hash != digest
            or page.ingestion_lease_owner != owner
            or page.ingestion_lease_token != cls._as_uuid(token)
        ):
            raise RuntimeError("ingestion lease 已失效，拒絕狀態變更")

    @staticmethod
    def _mark_ingestion_success(page: SitePage, digest: str) -> None:
        page.ingestion_status = "success"
        page.ingestion_content_hash = digest
        page.ingestion_attempt_hash = None
        page.ingestion_error = None
        SqlDocumentRepository._clear_ingestion_lease(page)

    @staticmethod
    def _clear_ingestion_lease(page: SitePage) -> None:
        page.ingestion_lease_owner = None
        page.ingestion_lease_token = None
        page.ingestion_lease_expires_at = None

    @staticmethod
    def _as_uuid(value: UUID | str) -> UUID:
        return value if isinstance(value, UUID) else UUID(str(value))

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def save(
        self,
        metadata: DocumentMetadata,
        raw_text: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
    ) -> None:
        self.save_idempotent(metadata, raw_text, chunks, embeddings)

    def save_idempotent(
        self,
        metadata: DocumentMetadata,
        raw_text: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
        *,
        page_id: UUID | str | None = None,
        lease_owner: str | None = None,
        lease_token: UUID | str | None = None,
        lease_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        if len(chunks) != len(embeddings):
            raise ValueError("chunk 與 embedding 數量不一致")
        url = str(metadata.source_url)
        with self._factory.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                # Serialize the same URL across processes before checking the
                # version/current rows.  The unique constraints remain the
                # final guard, while this lock prevents a lost current-version
                # update race.
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": url},
                )
            try:
                with session.begin_nested():
                    if page_id is not None:
                        if lease_owner is None or lease_token is None:
                            raise RuntimeError("ingestion lease context 不完整")
                        page = self._locked_ingestion_page(
                            session,
                            page_id=page_id,
                            canonical_url=url,
                        )
                        # The ingestion lease is a second fence inside the
                        # page crawl lease.  A competing process may renew the
                        # crawl lease while this worker is embedding; without
                        # this check it could still create a duplicate current
                        # document after its ingestion lease was stolen.
                        self._assert_ingestion_lease(
                            page,
                            digest=content_hash(raw_text),
                            owner=lease_owner,
                            token=lease_token,
                            now=now or datetime.now(timezone.utc),
                        )
                    if (
                        session.scalar(
                            select(Document.id).where(
                                Document.canonical_url == url,
                                Document.content_hash == content_hash(raw_text),
                            )
                        )
                        is not None
                    ):
                        return False
                    source = get_or_create_source(
                        session,
                        name=f"document:{metadata.unit}",
                        base_url=_base_url(url),
                        unit=metadata.unit,
                        source_type="official_document",
                    )
                    current = session.scalar(
                        select(Document).where(
                            Document.canonical_url == url,
                            Document.is_current.is_(True),
                        )
                    )
                    if current:
                        current.is_current = False
                    document = Document(
                        source_id=source.id,
                        title=metadata.title,
                        canonical_url=url,
                        document_type=metadata.document_type,
                        published_at=metadata.published_at,
                        effective_from=metadata.effective_from,
                        effective_to=metadata.effective_to,
                        version=metadata.version,
                        content_hash=content_hash(raw_text),
                        raw_text=raw_text,
                        is_current=True,
                        supersedes_document_id=current.id if current else None,
                    )
                    session.add(document)
                    session.flush()
                    session.add_all(
                        DocumentChunk(
                            document_id=document.id,
                            sequence=chunk.sequence,
                            content=chunk.content,
                            embedding=embedding,
                            token_count=chunk.token_count,
                        )
                        for chunk, embedding in zip(chunks, embeddings, strict=True)
                    )
                return True
            except IntegrityError:
                if (
                    session.scalar(
                        select(Document.id).where(
                            Document.canonical_url == url,
                            Document.content_hash == content_hash(raw_text),
                        )
                    )
                    is not None
                ):
                    return False
                raise


def _upsert_announcement(
    session: Session,
    candidate: AnnouncementCandidate,
    source: Source,
    now: datetime,
    completeness: tuple[int, ...] | tuple[bool, bool, bool] | None = None,
    advance_last_crawled_at: bool = True,
) -> str:
    digest = content_hash("\n".join([candidate.title, candidate.body]))
    existing = session.scalar(
        select(Announcement)
        .where(Announcement.canonical_url == candidate.canonical_url)
        .with_for_update()
    )
    if existing:
        incoming_is_weaker = (
            completeness is not None and len(existing.body.strip()) > completeness[0]
        )
        if incoming_is_weaker:
            if advance_last_crawled_at:
                existing.last_crawled_at = now
            return "unchanged"
        if not incoming_is_weaker:
            existing.title = candidate.title
            existing.unit = candidate.unit
            existing.category = candidate.category
            existing.published_at = candidate.published_at
            existing.deadline_at = candidate.deadline_at
            existing.body = candidate.body
            existing.warning = candidate.warning
        if advance_last_crawled_at:
            existing.last_crawled_at = now
        if existing.content_hash == digest:
            return "unchanged"
        existing.content_hash = digest
        return "updated"
    try:
        with session.begin_nested():
            session.add(
                Announcement(
                    source_id=source.id,
                    title=candidate.title,
                    unit=candidate.unit,
                    category=candidate.category,
                    published_at=candidate.published_at,
                    deadline_at=candidate.deadline_at,
                    canonical_url=candidate.canonical_url,
                    body=candidate.body,
                    warning=candidate.warning,
                    content_hash=digest,
                    last_crawled_at=now,
                )
            )
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(Announcement)
            .where(Announcement.canonical_url == candidate.canonical_url)
            .with_for_update()
        )
        if existing is None:
            raise
        return _upsert_announcement(
            session,
            candidate,
            source,
            now,
            completeness=completeness,
            advance_last_crawled_at=advance_last_crawled_at,
        )
    return "created"


class SqlAnnouncementRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def latest_crawled_at(self, source_name: str) -> datetime | None:
        with self._factory() as session:
            return session.scalar(
                select(Source.last_successful_crawl_at).where(
                    Source.name == source_name
                )
            )

    def canonical_urls_for_source(self, source_name: str) -> tuple[str, ...] | None:
        with self._factory() as session:
            source = session.scalar(select(Source).where(Source.name == source_name))
            if source is None or source.last_successful_crawl_at is None:
                return None
            return tuple(source.canonical_urls)

    def record_source_refresh(
        self,
        *,
        source_name: str,
        source_url: str,
        unit: str,
        interval_minutes: int,
        canonical_urls: tuple[str, ...],
        crawled_at: datetime,
    ) -> None:
        with self._factory.begin() as session:
            source = get_or_create_source(
                session,
                name=source_name,
                base_url=_base_url(source_url),
                unit=unit,
                source_type="announcement",
                crawl_enabled=True,
                crawl_interval_minutes=interval_minutes,
            )
            if (
                source.last_successful_crawl_at is not None
                and source.last_successful_crawl_at > crawled_at
            ):
                return
            source.canonical_urls = list(dict.fromkeys(canonical_urls))
            source.last_successful_crawl_at = crawled_at

    def try_acquire_source_refresh_lease(
        self, source_name: str
    ) -> SourceRefreshLease | _NoopSourceRefreshLease | None:
        """Fence a whole source crawl before it performs any external HTTP.

        This is deliberately a session-scoped PostgreSQL advisory lock rather
        than a transaction lock: the lease spans listing/detail fetches and is
        released in ``finally`` by the refresh coordinator.  Persisted source
        snapshots retain their transaction lock and monotonic watermark check.
        """

        session = self._factory()
        if session.get_bind().dialect.name != "postgresql":
            session.close()
            return _NoopSourceRefreshLease()
        key = f"nptu-source-refresh:{source_name}"
        try:
            acquired = bool(
                session.scalar(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
            )
        except Exception:
            session.close()
            raise
        if not acquired:
            session.close()
            return None
        return SourceRefreshLease(session, key)

    def upsert(
        self,
        candidate: AnnouncementCandidate,
        *,
        source_name: str,
        source_url: str,
        interval_minutes: int,
    ) -> str:
        now = datetime.now(timezone.utc)
        with self._factory.begin() as session:
            source = get_or_create_source(
                session,
                name=source_name,
                base_url=_base_url(source_url),
                unit=candidate.unit,
                source_type="announcement",
                crawl_enabled=True,
                crawl_interval_minutes=interval_minutes,
            )
            return _upsert_announcement(session, candidate, source, now)

    def upsert_many(
        self,
        candidates: list[AnnouncementCandidate],
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
    ) -> list[str]:
        return self.commit_source_refresh(
            candidates,
            source_name=source_name,
            source_url=source_url,
            source_unit=source_unit,
            interval_minutes=interval_minutes,
            crawled_at=datetime.now(timezone.utc),
        )

    def commit_source_refresh(
        self,
        candidates: list[AnnouncementCandidate],
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
        crawled_at: datetime,
    ) -> list[str]:
        with self._factory.begin() as session:
            source = get_or_create_source(
                session,
                name=source_name,
                base_url=_base_url(source_url),
                unit=source_unit,
                source_type="announcement",
                crawl_enabled=True,
                crawl_interval_minutes=interval_minutes,
            )
            if (
                source.last_successful_crawl_at is not None
                and source.last_successful_crawl_at > crawled_at
            ):
                return ["unchanged"] * len(candidates)
            results = [
                _upsert_announcement(session, candidate, source, crawled_at)
                for candidate in candidates
            ]
            source.canonical_urls = list(
                dict.fromkeys(candidate.canonical_url for candidate in candidates)
            )
            source.last_successful_crawl_at = crawled_at
            return results

    def merge_source_announcements(
        self,
        candidates: list[AnnouncementCandidate],
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
        crawled_at: datetime,
        advance_freshness: bool = False,
    ) -> list[str]:
        """Upsert a bounded scoped result without evicting the source cache.

        A partially fetched listing may persist the candidates that were
        successfully parsed, but it must not advance the source freshness
        watermark.  Otherwise a later DB-first query could treat an incomplete
        source as fresh and incorrectly skip recovery.
        """
        with self._factory.begin() as session:
            source = get_or_create_source(
                session,
                name=source_name,
                base_url=_base_url(source_url),
                unit=source_unit,
                source_type="announcement",
                crawl_enabled=True,
                crawl_interval_minutes=interval_minutes,
            )
            results = [
                _upsert_announcement(
                    session,
                    candidate,
                    source,
                    crawled_at,
                    advance_last_crawled_at=advance_freshness,
                )
                for candidate in candidates
            ]
            if advance_freshness:
                # Only a caller that has parsed a whole listing may advance a
                # source snapshot.  Scoped/live subsets must leave both the
                # watermark and its canonical URL snapshot untouched.
                source.canonical_urls = list(
                    dict.fromkeys(candidate.canonical_url for candidate in candidates)
                )
                source.last_successful_crawl_at = crawled_at
            return results

    def upsert_incremental_announcement(
        self,
        candidate: AnnouncementCandidate,
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
        crawled_at: datetime,
        completeness: tuple[int, ...] | tuple[bool, bool, bool] | None = None,
        page_id: UUID | str | None = None,
        lease_owner: str | None = None,
        lease_token: UUID | str | None = None,
        lease_expires_at: datetime | None = None,
        page_content_hash: str | None = None,
        source_page_url: str | None = None,
    ) -> str:
        """Persist one crawler announcement behind the page lease fence.

        This deliberately does not advance ``last_successful_crawl_at``;
        callers may be processing a partial listing and must only advance the
        source refresh marker after the whole batch has succeeded.
        """
        del lease_expires_at
        checked_at = crawled_at
        with self._factory.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": candidate.canonical_url},
                )
            if page_id is not None:
                if lease_owner is None or lease_token is None:
                    raise RuntimeError("ingestion lease context 不完整")
                page = SqlDocumentRepository._locked_ingestion_page(
                    session,
                    page_id=page_id,
                    canonical_url=source_page_url or candidate.canonical_url,
                )
                SqlDocumentRepository._assert_ingestion_lease(
                    page,
                    digest=page_content_hash or page.ingestion_content_hash or "",
                    owner=lease_owner,
                    token=lease_token,
                    now=checked_at,
                )
            source = get_or_create_source(
                session,
                name=source_name,
                base_url=_base_url(source_url),
                unit=source_unit,
                source_type="announcement",
                crawl_enabled=True,
                crawl_interval_minutes=interval_minutes,
            )
            return _upsert_announcement(
                session,
                candidate,
                source,
                checked_at,
                completeness=completeness,
            )

    def mark_incremental_source_success(
        self,
        *,
        source_name: str,
        crawled_at: datetime,
    ) -> None:
        with self._factory.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"nptu-source:{source_name}"},
                )
            source = session.scalar(
                select(Source).where(Source.name == source_name).with_for_update()
            )
            if source is not None and (
                source.last_successful_crawl_at is None
                or crawled_at > source.last_successful_crawl_at
            ):
                source.last_successful_crawl_at = crawled_at

    def list(
        self,
        *,
        q: str | None,
        unit: str | None,
        date_from: date | None,
        date_to: date | None,
        page: int,
        page_size: int,
    ) -> AnnouncementListResponse:
        filters: list[ColumnElement[bool]] = []
        if q:
            filters.append(
                Announcement.title.ilike(f"%{q}%") | Announcement.body.ilike(f"%{q}%")
            )
        if unit:
            filters.append(Announcement.unit == unit)
        if date_from:
            filters.append(Announcement.published_at >= date_from)
        if date_to:
            filters.append(Announcement.published_at <= date_to)
        with self._factory() as session:
            total = (
                session.scalar(
                    select(func.count()).select_from(Announcement).where(*filters)
                )
                or 0
            )
            rows = session.scalars(
                select(Announcement)
                .where(*filters)
                .order_by(Announcement.published_at.desc(), Announcement.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        return AnnouncementListResponse(
            items=[
                AnnouncementItem(
                    id=str(item.id),
                    title=item.title,
                    unit=item.unit,
                    category=item.category,
                    published_at=item.published_at,
                    deadline_at=item.deadline_at,
                    canonical_url=item.canonical_url,
                )
                for item in rows
            ],
            page=page,
            page_size=page_size,
            total=total,
        )
