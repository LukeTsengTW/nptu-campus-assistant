"""Bounded PostgreSQL completeness metadata queries.

These queries deliberately run after normal retrieval and operate on the small,
deduplicated evidence URL set.  They never inspect crawl attempt history and do
not change the retrieval ranking SQL.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.crawlers.official_units import DocumentSearchScope
from nptu_assistant.crawlers.site_models import SearchDeadline
from nptu_assistant.db.models import Announcement, Document, SitePage, Source
from nptu_assistant.rag.completeness import CompletenessFacts
from nptu_assistant.rag.models import Evidence


DocumentFactRow = tuple[
    str,
    bool,
    str | None,
    str | None,
    datetime | None,
    str | None,
    str | None,
    str | None,
    str | None,
    datetime | None,
    datetime | None,
]
AnnouncementFactRow = tuple[
    str,
    str | None,
    datetime | None,
    list[str] | None,
    datetime | None,
    bool,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    datetime | None,
    datetime | None,
]
SourceFreshnessRow = tuple[str, str, datetime | None]
AnnouncementSourceTarget = tuple[str, str, str]


def document_fact_rows_statement(urls: Collection[str]):
    """Return the production statement used for document completeness facts."""

    return (
        select(
            Document.canonical_url,
            Document.is_current,
            Source.unit.label("source_unit"),
            SitePage.page_type,
            SitePage.last_successful_crawl_at,
            SitePage.content_hash,
            SitePage.ingestion_content_hash,
            SitePage.ingestion_status,
            SitePage.announcement_ingestion_status,
            SitePage.crawl_lease_expires_at,
            SitePage.ingestion_lease_expires_at,
        )
        .join(Source, Source.id == Document.source_id)
        .outerjoin(
            SitePage,
            SitePage.canonical_url == Document.canonical_url,
        )
        .where(Document.canonical_url.in_(tuple(urls)))
    )


class SqlRetrievalCompletenessFacts:
    """Collects completeness facts in at most two statements per query kind."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        announcement_source_targets: Collection[AnnouncementSourceTarget] = (),
        max_announcement_source_targets: int = 200,
    ) -> None:
        if max_announcement_source_targets < 1:
            raise ValueError("max_announcement_source_targets 必須至少為 1")
        self._factory = factory
        targets = tuple(dict.fromkeys(announcement_source_targets))
        self._announcement_source_target_overflow = (
            len(targets) > max_announcement_source_targets
        )
        self._announcement_source_targets = targets[:max_announcement_source_targets]

    def document_facts(
        self,
        evidence: Collection[Evidence],
        *,
        scope: DocumentSearchScope | None,
        now: datetime,
        strong_score: float,
        min_content_chars: int,
        soft_stale: timedelta,
        hard_stale: timedelta,
        deadline: SearchDeadline | None = None,
    ) -> CompletenessFacts:
        urls = _urls(evidence)
        try:
            with self._factory() as session:
                _apply_deadline(session, deadline)
                rows: list[DocumentFactRow] = (
                    cast(
                        list[DocumentFactRow],
                        session.execute(document_fact_rows_statement(urls)).all(),
                    )
                    if urls
                    else []
                )
                if scope is not None and scope.canonical_unit:
                    _apply_deadline(session, deadline)
                    scope_count = session.scalar(
                        select(Document.id)
                        .join(Source, Source.id == Document.source_id)
                        .where(
                            Document.is_current.is_(True),
                            Source.unit == scope.canonical_unit,
                        )
                        .limit(1)
                    )
                else:
                    scope_count = None
        except Exception:
            return CompletenessFacts(
                evidence_count=len(evidence),
                unique_url_count=len(urls),
                facts_query_succeeded=False,
                canonical_urls=urls,
            )
        return _document_facts_from_rows(
            evidence,
            rows,
            urls=urls,
            scope=scope,
            scope_has_current_document=scope_count is not None,
            now=now,
            strong_score=strong_score,
            min_content_chars=min_content_chars,
            soft_stale=soft_stale,
            hard_stale=hard_stale,
        )

    def announcement_facts(
        self,
        evidence: Collection[Evidence],
        *,
        unit: str | None,
        now: datetime,
        strong_score: float,
        min_content_chars: int,
        soft_stale: timedelta,
        hard_stale: timedelta,
        source_target_limit: int = 20,
        deadline: SearchDeadline | None = None,
    ) -> CompletenessFacts:
        del source_target_limit
        urls = _urls(evidence)
        if self._announcement_source_target_overflow:
            return CompletenessFacts(
                evidence_count=len(evidence),
                unique_url_count=len(urls),
                facts_query_succeeded=False,
                canonical_urls=urls,
            )
        configured_targets = tuple(
            target
            for target in self._announcement_source_targets
            if unit is None or target[2] == unit
        )
        try:
            with self._factory() as session:
                _apply_deadline(session, deadline)
                rows: list[AnnouncementFactRow] = (
                    cast(
                        list[AnnouncementFactRow],
                        session.execute(
                            select(
                                Announcement.canonical_url,
                                Source.unit.label("source_unit"),
                                Source.last_successful_crawl_at,
                                Source.canonical_urls,
                                Announcement.last_crawled_at,
                                SitePage.id.is_not(None).label("has_site_page"),
                                SitePage.content_hash,
                                SitePage.ingestion_content_hash,
                                SitePage.ingestion_status,
                                SitePage.announcement_ingestion_status,
                                Announcement.warning,
                                SitePage.crawl_lease_expires_at,
                                SitePage.ingestion_lease_expires_at,
                            )
                            .join(Source, Source.id == Announcement.source_id)
                            .outerjoin(
                                SitePage,
                                SitePage.canonical_url == Announcement.canonical_url,
                            )
                            .where(Announcement.canonical_url.in_(urls))
                        ).all(),
                    )
                    if urls
                    else []
                )
                source_query = select(
                    Source.name,
                    Source.base_url,
                    Source.last_successful_crawl_at,
                ).where(
                    Source.crawl_enabled.is_(True),
                    Source.source_type == "announcement",
                )
                if configured_targets:
                    source_query = source_query.where(
                        Source.name.in_(
                            tuple(target[0] for target in configured_targets)
                        )
                    )
                elif unit:
                    source_query = source_query.where(Source.unit == unit)
                _apply_deadline(session, deadline)
                source_rows = cast(
                    list[SourceFreshnessRow],
                    session.execute(source_query.order_by(Source.name)).all(),
                )
        except Exception:
            return CompletenessFacts(
                evidence_count=len(evidence),
                unique_url_count=len(urls),
                facts_query_succeeded=False,
                canonical_urls=urls,
            )
        return _announcement_facts_from_rows(
            evidence,
            rows,
            source_rows,
            configured_targets=configured_targets,
            urls=urls,
            unit=unit,
            now=now,
            strong_score=strong_score,
            min_content_chars=min_content_chars,
            soft_stale=soft_stale,
            hard_stale=hard_stale,
        )


def _urls(evidence: Collection[Evidence]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.url for item in evidence if item.url))


def _raise_if_expired(deadline: SearchDeadline | None) -> None:
    if deadline is not None:
        deadline.raise_if_expired()


def _apply_deadline(session: Session, deadline: SearchDeadline | None) -> None:
    """Bound every metadata query without adding a per-evidence statement.

    The facts collector has at most two data statements.  A transaction-local
    PostgreSQL timeout is refreshed from the *remaining* request budget before
    each statement, so a first query cannot leave a stale, larger timeout for
    the second one.
    """

    if deadline is None:
        return
    deadline.raise_if_expired()
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    remaining_ms = max(1, int(deadline.remaining_seconds() * 1_000))
    session.execute(text(f"SET LOCAL statement_timeout = {remaining_ms}"))


def _evidence_quality(
    evidence: Collection[Evidence],
    *,
    strong_score: float,
    min_content_chars: int,
) -> tuple[int, float, float]:
    scores = sorted((max(0.0, item.score) for item in evidence), reverse=True)
    strong = sum(
        item.score >= strong_score and len(item.content.strip()) >= min_content_chars
        for item in evidence
    )
    top = scores[0] if scores else 0.0
    margin = top - scores[1] if len(scores) > 1 else top
    return strong, top, margin


def _document_facts_from_rows(
    evidence: Collection[Evidence],
    rows: Collection[DocumentFactRow],
    *,
    urls: tuple[str, ...],
    scope: DocumentSearchScope | None,
    scope_has_current_document: bool,
    now: datetime,
    strong_score: float,
    min_content_chars: int,
    soft_stale: timedelta,
    hard_stale: timedelta,
) -> CompletenessFacts:
    strong, top, margin = _evidence_quality(
        evidence,
        strong_score=strong_score,
        min_content_chars=min_content_chars,
    )
    current = superseded = fresh = soft = hard = in_sync = pending = failed = 0
    exact = active = incomplete = homepage = listing = 0
    seen_current_urls: set[str] = set()
    for row in rows:
        (
            url,
            is_current,
            source_unit,
            page_type,
            last_success,
            content_hash,
            ingestion_hash,
            ingestion_status,
            announcement_status,
            crawl_lease_expires,
            ingestion_lease_expires,
        ) = row
        if not is_current:
            superseded += 1
            continue
        if url in seen_current_urls:
            continue
        seen_current_urls.add(url)
        current += 1
        if page_type == "unit_homepage":
            homepage += 1
        elif page_type == "announcement_listing":
            listing += 1
        if scope is not None and source_unit == scope.canonical_unit:
            exact += 1
        if content_hash and content_hash == ingestion_hash:
            in_sync += 1
        if ingestion_status == "pending":
            pending += 1
        elif ingestion_status in {"failed", "partial"}:
            failed += 1
        if announcement_status == "incomplete":
            incomplete += 1
        if (crawl_lease_expires and crawl_lease_expires > now) or (
            ingestion_lease_expires and ingestion_lease_expires > now
        ):
            active += 1
        age = now - last_success if last_success is not None else None
        if age is None or age > hard_stale:
            hard += 1
        elif age > soft_stale:
            soft += 1
        else:
            fresh += 1
    if scope is not None and scope_has_current_document and exact == 0:
        # A scoped source exists but the retrieved evidence misses it.
        exact = 0
    return CompletenessFacts(
        evidence_count=len(evidence),
        unique_url_count=len(urls),
        strong_evidence_count=strong,
        top_score=top,
        score_margin=margin,
        current_document_count=current,
        superseded_document_count=superseded,
        exact_scope_match_count=exact,
        homepage_only_count=homepage if current == homepage else 0,
        listing_only_count=listing if current == listing else 0,
        fresh_count=fresh,
        soft_stale_count=soft,
        hard_stale_count=hard,
        content_hash_in_sync_count=in_sync,
        pending_ingestion_count=pending,
        failed_ingestion_count=failed,
        incomplete_announcement_count=incomplete,
        active_refresh_count=active,
        source_coverage_ratio=1.0 if current else 0.0,
        canonical_urls=urls,
    )


def _announcement_facts_from_rows(
    evidence: Collection[Evidence],
    rows: Collection[AnnouncementFactRow],
    source_rows: Collection[SourceFreshnessRow],
    *,
    configured_targets: Collection[AnnouncementSourceTarget],
    urls: tuple[str, ...],
    unit: str | None,
    now: datetime,
    strong_score: float,
    min_content_chars: int,
    soft_stale: timedelta,
    hard_stale: timedelta,
) -> CompletenessFacts:
    strong, top, margin = _evidence_quality(
        evidence,
        strong_score=strong_score,
        min_content_chars=min_content_chars,
    )
    fresh = soft = hard = pending = failed = incomplete = active = exact = 0
    current_snapshot_urls: set[str] = set()
    for row in rows:
        (
            url,
            source_unit,
            _source_last_success,
            source_canonical_urls,
            announcement_last_crawled_at,
            has_site_page,
            content_hash,
            ingestion_hash,
            ingestion_status,
            announcement_status,
            announcement_warning,
            crawl_lease_expires,
            ingestion_lease_expires,
        ) = row
        if url in current_snapshot_urls:
            continue
        # Announcement sources persist their most recently successful listing
        # snapshot atomically.  A detail that is absent from that snapshot is
        # historical evidence, not proof that a "latest" request is complete.
        if url not in set(source_canonical_urls or ()):
            continue
        current_snapshot_urls.add(url)
        if unit is not None and source_unit == unit:
            exact += 1
        if ingestion_status == "pending" or announcement_status == "pending":
            pending += 1
        elif (
            announcement_warning
            or ingestion_status in {"failed", "partial"}
            or announcement_status in {"failed", "partial"}
        ):
            failed += 1
        if announcement_status == "incomplete":
            incomplete += 1
        if (crawl_lease_expires and crawl_lease_expires > now) or (
            ingestion_lease_expires and ingestion_lease_expires > now
        ):
            active += 1
        # A fresh listing alone does not make an old persisted detail fresh:
        # successful upserts advance Announcement.last_crawled_at, so use that
        # item-level timestamp for evidence freshness and Source for coverage.
        age = (
            now - announcement_last_crawled_at
            if announcement_last_crawled_at is not None
            else None
        )
        if age is None or age > hard_stale:
            hard += 1
        elif age > soft_stale:
            soft += 1
        else:
            fresh += 1
    in_sync = sum(
        has_site_page
        and content_hash is not None
        and (
            content_hash == ingestion_hash
            and ingestion_status in {"success", "partial"}
            and announcement_status in {"success", "incomplete", "not_applicable"}
        )
        for (
            _url,
            _source_unit,
            _last_success,
            _source_canonical_urls,
            _announcement_last_crawled_at,
            has_site_page,
            content_hash,
            ingestion_hash,
            ingestion_status,
            announcement_status,
            _announcement_warning,
            _crawl_lease_expires,
            _ingestion_lease_expires,
        ) in rows
    )
    freshness_by_name = {name: last_success for name, _url, last_success in source_rows}
    source_count = len(configured_targets) or len(source_rows)
    if configured_targets:
        fresh_sources = 0
        for name, _url, _unit in configured_targets:
            last_success = freshness_by_name.get(name)
            if last_success is not None and now - last_success <= soft_stale:
                fresh_sources += 1
    else:
        fresh_sources = sum(
            last_success is not None and now - last_success <= soft_stale
            for _name, _url, last_success in source_rows
        )
    targets = tuple(
        dict.fromkeys(
            (
                *urls,
                *(url for _name, url, _unit in configured_targets),
                *(url for _name, url, _at in source_rows),
            )
        )
    )
    return CompletenessFacts(
        evidence_count=len(evidence),
        unique_url_count=len(urls),
        strong_evidence_count=strong,
        top_score=top,
        score_margin=margin,
        current_document_count=len(current_snapshot_urls),
        exact_scope_match_count=exact,
        fresh_count=fresh,
        soft_stale_count=soft,
        hard_stale_count=hard,
        content_hash_in_sync_count=in_sync,
        pending_ingestion_count=pending,
        failed_ingestion_count=failed,
        incomplete_announcement_count=incomplete,
        active_refresh_count=active,
        source_coverage_ratio=(fresh_sources / source_count if source_count else 0.0),
        canonical_urls=targets,
        source_names=tuple(
            dict.fromkeys(
                [
                    *(name for name, _url, _unit in configured_targets),
                    *(name for name, _url, _at in source_rows),
                ]
            )
        ),
    )
