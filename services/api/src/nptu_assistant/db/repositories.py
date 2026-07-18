from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.api.schemas import AnnouncementItem, AnnouncementListResponse
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.db.models import Announcement, Document, DocumentChunk, Source
from nptu_assistant.ingestion.chunking import TextChunk
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata


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
    source = session.scalar(select(Source).where(Source.name == name))
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

    def save(
        self,
        metadata: DocumentMetadata,
        raw_text: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunk 與 embedding 數量不一致")
        url = str(metadata.source_url)
        with self._factory.begin() as session:
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


def _upsert_announcement(
    session: Session,
    candidate: AnnouncementCandidate,
    source: Source,
    now: datetime,
) -> str:
    digest = content_hash("\n".join([candidate.title, candidate.body]))
    existing = session.scalar(
        select(Announcement).where(
            Announcement.canonical_url == candidate.canonical_url
        )
    )
    if existing:
        existing.title = candidate.title
        existing.unit = candidate.unit
        existing.category = candidate.category
        existing.published_at = candidate.published_at
        existing.deadline_at = candidate.deadline_at
        existing.body = candidate.body
        existing.warning = candidate.warning
        existing.last_crawled_at = now
        if existing.content_hash == digest:
            return "unchanged"
        existing.content_hash = digest
        return "updated"
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
            source.canonical_urls = list(dict.fromkeys(canonical_urls))
            source.last_successful_crawl_at = crawled_at

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
            results = [
                _upsert_announcement(session, candidate, source, crawled_at)
                for candidate in candidates
            ]
            source.canonical_urls = list(
                dict.fromkeys(candidate.canonical_url for candidate in candidates)
            )
            source.last_successful_crawl_at = crawled_at
            return results

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
        filters = []
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
