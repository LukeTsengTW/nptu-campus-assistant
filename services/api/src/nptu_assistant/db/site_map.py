from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import and_, case, false, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session, selectinload, sessionmaker

from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_nptu_url
from nptu_assistant.crawlers.official_units import DocumentSearchScope
from nptu_assistant.crawlers.site_map import (
    DISCOVERY_SOURCE_PRIORITY,
    PAGE_TYPE_PRIORITY,
    SiteCrawlStatus,
    SiteDiscoverySource,
    SiteLinkType,
    SiteMapCandidate,
    SiteMapRepository,
    SiteMapSyncSummary,
    SiteMapWriteResult,
    SitePageType,
    SitePageUpsert,
    source_priority,
)
from nptu_assistant.crawlers.site_models import SearchPlan
from nptu_assistant.db.models import Announcement, Document, SiteLink, SitePage, Source


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _host_scope(hosts: Collection[str]) -> Any:
    conditions = []
    for host in hosts:
        normalized = host.strip().lower().rstrip(".")
        if not normalized:
            continue
        conditions.append(
            or_(SitePage.host == normalized, SitePage.host.like(f"%.{normalized}"))
        )
    return or_(*conditions) if conditions else false()


def _source_rank_expression(column: Any) -> Any:
    return case(
        *(
            (column == source.value, priority)
            for source, priority in DISCOVERY_SOURCE_PRIORITY.items()
        ),
        else_=0,
    )


def _is_specific_page_type(column: Any) -> Any:
    return column.in_(
        [
            SitePageType.UNIT_HOMEPAGE.value,
            SitePageType.ANNOUNCEMENT_LISTING.value,
            SitePageType.ANNOUNCEMENT_DETAIL.value,
            SitePageType.OFFICIAL_DOCUMENT.value,
        ]
    )


class SqlSiteMapRepository(SiteMapRepository):
    """以 DB unique constraint 為最終去重保證的網頁地圖 repository。"""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._factory = factory
        self._clock = clock

    def upsert_page(self, page: SitePageUpsert) -> SiteMapWriteResult:
        now = self._clock()
        with self._factory.begin() as session:
            existing = session.scalar(
                select(SitePage.id).where(SitePage.canonical_url == page.canonical_url)
            )
            self._upsert_page_in_session(session, page, now=now)
            return SiteMapWriteResult(created=existing is None, updated=existing is not None)

    def upsert_link(
        self,
        source: SitePageUpsert,
        target: SitePageUpsert,
        *,
        anchor_text: str,
        link_type: SiteLinkType,
    ) -> SiteMapWriteResult:
        now = self._clock()
        with self._factory.begin() as session:
            self._upsert_page_in_session(session, source, now=now)
            self._upsert_page_in_session(session, target, now=now)
            source_page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == source.canonical_url)
            )
            target_page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == target.canonical_url)
            )
            if source_page is None or target_page is None:
                raise RuntimeError("site map link 的 source/target page 建立失敗")
            existing = session.scalar(
                select(SiteLink.id).where(
                    SiteLink.source_page_id == source_page.id,
                    SiteLink.target_page_id == target_page.id,
                )
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = postgres_insert(SiteLink).values(
                    id=SiteLink.id.default.arg(),  # type: ignore[union-attr]
                    source_page_id=source_page.id,
                    target_page_id=target_page.id,
                    anchor_text=anchor_text,
                    link_type=link_type.value,
                    first_discovered_at=now,
                    last_discovered_at=now,
                )
                excluded = statement.excluded
                statement = statement.on_conflict_do_update(
                    constraint="uq_site_links_source_target",
                    set_={
                        "anchor_text": case(
                            (excluded.anchor_text != "", excluded.anchor_text),
                            else_=SiteLink.anchor_text,
                        ),
                        "link_type": case(
                            (
                                SiteLink.link_type == SiteLinkType.UNKNOWN.value,
                                excluded.link_type,
                            ),
                            else_=SiteLink.link_type,
                        ),
                        "last_discovered_at": func.greatest(
                            SiteLink.last_discovered_at,
                            excluded.last_discovered_at,
                        ),
                        "updated_at": func.now(),
                    },
                )
                session.execute(statement)
            else:
                if existing is None:
                    session.add(
                        SiteLink(
                            source_page_id=source_page.id,
                            target_page_id=target_page.id,
                            anchor_text=anchor_text,
                            link_type=link_type.value,
                            first_discovered_at=now,
                            last_discovered_at=now,
                        )
                    )
                else:
                    link = session.get(SiteLink, existing)
                    if link is not None:
                        if anchor_text:
                            link.anchor_text = anchor_text
                        if link.link_type == SiteLinkType.UNKNOWN.value:
                            link.link_type = link_type.value
                        link.last_discovered_at = now
            return SiteMapWriteResult(created=existing is None, updated=existing is not None)

    def record_crawl_success(
        self,
        canonical_url: str,
        *,
        title: str | None,
        content_hash: str,
        http_status: int | None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SiteMapWriteResult:
        now = self._clock()
        with self._factory.begin() as session:
            page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == canonical_url)
            )
            created = page is None
            if page is None:
                self._upsert_page_in_session(
                    session,
                    SitePageUpsert(canonical_url=canonical_url),
                    now=now,
                )
                page = session.scalar(
                    select(SitePage).where(SitePage.canonical_url == canonical_url)
                )
            if page is None:
                raise RuntimeError("site map crawl success page 建立失敗")
            changed = page.content_hash != content_hash
            page.crawl_status = (
                SiteCrawlStatus.SUCCESS.value
                if changed
                else SiteCrawlStatus.UNCHANGED.value
            )
            page.http_status = http_status
            page.etag = etag
            page.last_modified = last_modified
            page.last_crawled_at = now
            page.last_successful_crawl_at = now
            page.failure_count = 0
            if changed:
                page.last_changed_at = now
            page.content_hash = content_hash
            if title and title.strip():
                page.title = title.strip()
            return SiteMapWriteResult(created=created, updated=not created)

    def record_crawl_failure(
        self,
        canonical_url: str,
        *,
        http_status: int | None = None,
        status: SiteCrawlStatus = SiteCrawlStatus.FAILED,
    ) -> SiteMapWriteResult:
        now = self._clock()
        with self._factory.begin() as session:
            page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == canonical_url)
            )
            created = page is None
            if page is None:
                self._upsert_page_in_session(
                    session,
                    SitePageUpsert(canonical_url=canonical_url),
                    now=now,
                )
                page = session.scalar(
                    select(SitePage).where(SitePage.canonical_url == canonical_url)
                )
            if page is None:
                raise RuntimeError("site map crawl failure page 建立失敗")
            page.crawl_status = status.value
            page.http_status = http_status
            page.last_crawled_at = now
            page.failure_count += 1
            return SiteMapWriteResult(created=created, updated=not created)

    def find_candidates(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope | None,
        allowed_hosts: Collection[str],
        limit: int,
    ) -> tuple[SiteMapCandidate, ...]:
        if limit <= 0 or not allowed_hosts:
            return ()
        query_text = plan.query[:500]
        pattern = f"%{query_text}%"
        text_match = or_(
            SitePage.title.ilike(pattern),
            SitePage.path.ilike(pattern),
        )
        unit_match = (
            SitePage.unit == scope.canonical_unit
            if scope is not None and scope.canonical_unit
            else literal(False)
        )
        preferred_match = (
            SitePage.host.in_(tuple(scope.preferred_hosts))
            if scope is not None and scope.preferred_hosts
            else literal(False)
        )
        intent = " ".join((plan.query, *plan.concepts)).casefold()
        announcement_intent = any(token in intent for token in ("公告", "通知", "訊息"))
        document_intent = any(token in intent for token in ("文件", "辦法", "表單", "規章"))
        page_type_score = case(
            (and_(literal(announcement_intent), SitePage.page_type == SitePageType.ANNOUNCEMENT_LISTING.value), 0.34),
            (and_(literal(announcement_intent), SitePage.page_type == SitePageType.ANNOUNCEMENT_DETAIL.value), 0.28),
            (and_(literal(document_intent), SitePage.page_type == SitePageType.OFFICIAL_DOCUMENT.value), 0.34),
            (SitePage.page_type == SitePageType.UNIT_HOMEPAGE.value, 0.24),
            else_=0.0,
        )
        relevance = (
            case((unit_match, 0.42), else_=0.0)
            + case((preferred_match, 0.18), else_=0.0)
            + case((text_match, 0.34), else_=0.06)
            + page_type_score
            + (func.least(SitePage.crawl_priority, 100) / 1_000.0)
            - (func.least(SitePage.failure_count, 5) * 0.04)
        )
        query = (
            select(SitePage, relevance.label("relevance"))
            .where(
                SitePage.is_active.is_(True),
                SitePage.is_indexable.is_(True),
                SitePage.crawl_status.not_in(
                    [SiteCrawlStatus.BLOCKED.value, SiteCrawlStatus.EXCLUDED.value]
                ),
                _host_scope(allowed_hosts),
            )
            .order_by(
                unit_match.desc(),
                preferred_match.desc(),
                relevance.desc(),
                SitePage.crawl_priority.desc(),
                SitePage.minimum_depth.asc(),
                SitePage.failure_count.asc(),
                SitePage.last_successful_crawl_at.desc().nulls_last(),
            )
            .limit(limit)
        )
        with self._factory() as session:
            rows = session.execute(query).all()
        return tuple(
            SiteMapCandidate(
                canonical_url=row[0].canonical_url,
                title=row[0].title,
                host=row[0].host,
                unit=row[0].unit,
                page_type=_page_type(row[0].page_type),
                crawl_priority=row[0].crawl_priority,
                minimum_depth=row[0].minimum_depth,
                failure_count=row[0].failure_count,
                relevance=float(row[1]),
            )
            for row in rows
        )

    def import_existing_urls(self) -> Mapping[str, SiteMapSyncSummary]:
        with self._factory() as session:
            sources = [
                (
                    item.base_url,
                    item.canonical_urls or [],
                    item.unit,
                    item.source_type,
                )
                for item in session.scalars(select(Source)).all()
            ]
            documents = [
                (item.canonical_url, item.title, item.source.unit, item.content_hash)
                for item in session.scalars(
                    select(Document).options(selectinload(Document.source))
                ).all()
                if item.source is not None
            ]
            announcements = [
                (
                    item.canonical_url,
                    item.title,
                    item.unit,
                    item.content_hash,
                )
                for item in session.scalars(
                    select(Announcement).options(selectinload(Announcement.source))
                ).all()
            ]

        result: dict[str, SiteMapSyncSummary] = {}
        for base_url, canonical_urls, unit, source_type in sources:
            bucket = result.setdefault("Source URLs", SiteMapSyncSummary())
            for url in (base_url, *canonical_urls):
                self._import_one(
                    bucket,
                    SitePageUpsert(
                        canonical_url=url,
                        unit=unit,
                        page_type=(
                            SitePageType.ANNOUNCEMENT_LISTING
                            if source_type == "announcement"
                            else SitePageType.GENERAL_PAGE
                        ),
                        discovery_source=SiteDiscoverySource.EXISTING_SOURCE,
                        crawl_priority=PAGE_TYPE_PRIORITY[
                            SitePageType.ANNOUNCEMENT_LISTING
                            if source_type == "announcement"
                            else SitePageType.GENERAL_PAGE
                        ],
                    ),
                )
        bucket = result.setdefault("Document URLs", SiteMapSyncSummary())
        for url, title, unit, digest in documents:
            self._import_one(
                bucket,
                SitePageUpsert(
                    canonical_url=url,
                    title=title,
                    unit=unit,
                    content_hash=digest,
                    page_type=SitePageType.OFFICIAL_DOCUMENT,
                    discovery_source=SiteDiscoverySource.EXISTING_DOCUMENT,
                    crawl_priority=PAGE_TYPE_PRIORITY[SitePageType.OFFICIAL_DOCUMENT],
                ),
            )
        bucket = result.setdefault("Announcement URLs", SiteMapSyncSummary())
        for url, title, unit, digest in announcements:
            self._import_one(
                bucket,
                SitePageUpsert(
                    canonical_url=url,
                    title=title,
                    unit=unit,
                    content_hash=digest,
                    page_type=SitePageType.ANNOUNCEMENT_DETAIL,
                    discovery_source=SiteDiscoverySource.EXISTING_ANNOUNCEMENT,
                    crawl_priority=PAGE_TYPE_PRIORITY[SitePageType.ANNOUNCEMENT_DETAIL],
                ),
            )
        return result

    def _import_one(self, summary: SiteMapSyncSummary, page: SitePageUpsert) -> None:
        summary.seen += 1
        try:
            normalized = canonicalize_nptu_url(page.canonical_url)
            if not is_allowed_nptu_url(normalized):
                summary.skipped += 1
                return
            self._add_import_result(
                summary,
                self.upsert_page(
                    SitePageUpsert(
                        canonical_url=normalized,
                        title=page.title,
                        unit=page.unit,
                        content_hash=page.content_hash,
                        page_type=page.page_type,
                        discovery_source=page.discovery_source,
                        crawl_priority=page.crawl_priority,
                        minimum_depth=page.minimum_depth,
                        is_indexable=page.is_indexable,
                    )
                ),
            )
        except Exception:
            summary.failed += 1

    @staticmethod
    def _add_import_result(
        summary: SiteMapSyncSummary,
        result: SiteMapWriteResult,
    ) -> None:
        summary.add(result)

    def _upsert_page_in_session(
        self,
        session: Session,
        page: SitePageUpsert,
        *,
        now: datetime,
    ) -> None:
        parsed = urlsplit(page.canonical_url)
        values = {
            "canonical_url": page.canonical_url,
            "host": (parsed.hostname or "").lower(),
            "path": parsed.path or "/",
            "title": page.title,
            "unit": page.unit,
            "content_hash": page.content_hash,
            "page_type": page.page_type.value,
            "discovery_source": page.discovery_source.value,
            "crawl_status": SiteCrawlStatus.DISCOVERED.value,
            "last_discovered_at": now,
            "crawl_priority": page.crawl_priority,
            "minimum_depth": page.minimum_depth,
            "is_indexable": page.is_indexable,
            "is_active": True,
        }
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = postgres_insert(SitePage).values(values)
            excluded = statement.excluded
            existing_source_rank = _source_rank_expression(SitePage.discovery_source)
            incoming_source_rank = _source_rank_expression(excluded.discovery_source)
            title_is_safe = and_(
                or_(SitePage.title.is_(None), SitePage.title == ""),
                excluded.title.is_not(None),
            )
            title_from_trusted_source = and_(
                SitePage.crawl_status.not_in(
                    [SiteCrawlStatus.SUCCESS.value, SiteCrawlStatus.UNCHANGED.value]
                ),
                excluded.title.is_not(None),
                incoming_source_rank >= existing_source_rank,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[SitePage.canonical_url],
                set_={
                    "title": case(
                        (title_is_safe, excluded.title),
                        (title_from_trusted_source, excluded.title),
                        else_=SitePage.title,
                    ),
                    "unit": func.coalesce(SitePage.unit, excluded.unit),
                    "content_hash": func.coalesce(
                        SitePage.content_hash,
                        excluded.content_hash,
                    ),
                    "page_type": case(
                        (
                            or_(
                                SitePage.page_type == SitePageType.UNKNOWN.value,
                                and_(
                                    SitePage.page_type == SitePageType.GENERAL_PAGE.value,
                                    _is_specific_page_type(excluded.page_type),
                                ),
                            ),
                            excluded.page_type,
                        ),
                        else_=SitePage.page_type,
                    ),
                    "discovery_source": case(
                        (incoming_source_rank > existing_source_rank, excluded.discovery_source),
                        else_=SitePage.discovery_source,
                    ),
                    "last_discovered_at": func.greatest(
                        SitePage.last_discovered_at,
                        excluded.last_discovered_at,
                    ),
                    "crawl_priority": func.greatest(
                        SitePage.crawl_priority,
                        excluded.crawl_priority,
                    ),
                    "minimum_depth": func.least(
                        SitePage.minimum_depth,
                        excluded.minimum_depth,
                    ),
                    "is_indexable": case(
                        (SitePage.is_indexable.is_(False), False),
                        else_=excluded.is_indexable,
                    ),
                    "updated_at": func.now(),
                },
            )
            session.execute(statement)
            return

        existing = session.scalar(
            select(SitePage).where(SitePage.canonical_url == page.canonical_url)
        )
        if existing is None:
            session.add(SitePage(**values))
            return
        if not existing.title or (
            existing.crawl_status not in {
                SiteCrawlStatus.SUCCESS.value,
                SiteCrawlStatus.UNCHANGED.value,
            }
            and source_priority(page.discovery_source)
            >= source_priority(_source(existing.discovery_source))
        ):
            existing.title = page.title or existing.title
        existing.unit = existing.unit or page.unit
        existing.content_hash = existing.content_hash or page.content_hash
        if existing.page_type == SitePageType.UNKNOWN.value or (
            existing.page_type == SitePageType.GENERAL_PAGE.value
            and page.page_type in {
                SitePageType.UNIT_HOMEPAGE,
                SitePageType.ANNOUNCEMENT_LISTING,
                SitePageType.ANNOUNCEMENT_DETAIL,
                SitePageType.OFFICIAL_DOCUMENT,
            }
        ):
            existing.page_type = page.page_type.value
        if source_priority(page.discovery_source) > source_priority(
            _source(existing.discovery_source)
        ):
            existing.discovery_source = page.discovery_source.value
        existing.last_discovered_at = max(existing.last_discovered_at, now)
        existing.crawl_priority = max(existing.crawl_priority, page.crawl_priority)
        existing.minimum_depth = min(existing.minimum_depth, page.minimum_depth)
        existing.is_indexable = existing.is_indexable and page.is_indexable


def _source(value: str) -> SiteDiscoverySource:
    try:
        return SiteDiscoverySource(value)
    except ValueError:
        return SiteDiscoverySource.MANUAL


def _page_type(value: str) -> SitePageType:
    try:
        return SitePageType(value)
    except ValueError:
        return SitePageType.UNKNOWN
