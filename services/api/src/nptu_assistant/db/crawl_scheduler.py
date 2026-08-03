from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, cast as sql_cast, func, insert, not_, or_, select, update
from sqlalchemy.orm import Session, aliased, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from nptu_assistant.crawlers.crawl_policy import NON_HTML_RESOURCE_SUFFIXES
from nptu_assistant.crawlers.crawl_scheduler import (
    CrawlClaim,
    FailureDecision,
)
from nptu_assistant.crawlers.site_map import FrontierPolicy, SiteCrawlStatus
from nptu_assistant.db.crawl_models import SiteCrawlAttempt
from nptu_assistant.db.models import SitePage


def _now() -> datetime:
    return datetime.now(timezone.utc)


def due_pages_statement(
    *,
    now: datetime,
    limit: int,
    frontier_policy: FrontierPolicy | None = None,
    urls: tuple[str, ...] = (),
):
    """Build the PostgreSQL claim query; kept public for SQL-level tests."""

    if limit <= 0:
        raise ValueError("claim 數量必須大於零")
    policy = frontier_policy or FrontierPolicy()
    candidate = aliased(SitePage, name="frontier_candidate")
    active = aliased(SitePage, name="frontier_active")
    non_html = or_(
        *(candidate.path.ilike(f"%{suffix}") for suffix in NON_HTML_RESOURCE_SUFFIXES)
    )
    active_count = (
        select(func.count(active.id))
        .where(
            active.host == candidate.host,
            active.is_active.is_(True),
            active.crawl_lease_expires_at > now,
            active.id != candidate.id,
        )
        .correlate(candidate)
        .scalar_subquery()
    )
    due_filters = (
        candidate.is_active.is_(True),
        candidate.is_indexable.is_(True),
        not_(non_html),
        candidate.crawl_status.not_in(
            [
                SiteCrawlStatus.BLOCKED.value,
                SiteCrawlStatus.EXCLUDED.value,
            ]
        ),
        candidate.next_crawl_at.is_not(None),
        candidate.next_crawl_at <= now,
        or_(
            candidate.crawl_lease_expires_at.is_(None),
            candidate.crawl_lease_expires_at <= now,
        ),
        candidate.minimum_depth <= policy.max_depth,
        active_count < policy.per_host_active_cap,
    )
    if urls:
        due_filters = (*due_filters, candidate.canonical_url.in_(urls))
    host_rank = func.row_number().over(
        partition_by=candidate.host,
        order_by=(
            candidate.crawl_priority.desc(),
            candidate.next_crawl_at.asc().nulls_first(),
            candidate.minimum_depth.asc(),
            candidate.failure_count.asc(),
            candidate.id.asc(),
        ),
    )
    ranked = (
        select(
            candidate.id.label("page_id"),
            host_rank.label("host_rank"),
            active_count.label("active_count"),
        )
        .where(*due_filters)
        .subquery("ranked_frontier")
    )
    available_host_slots = func.least(
        policy.per_host_due_cap,
        policy.per_host_active_cap - ranked.c.active_count,
    )
    return (
        select(SitePage)
        .join(ranked, ranked.c.page_id == SitePage.id)
        .where(ranked.c.host_rank <= available_host_slots)
        .order_by(
            ranked.c.host_rank.asc(),
            SitePage.crawl_priority.desc(),
            SitePage.next_crawl_at.asc().nulls_first(),
            SitePage.minimum_depth.asc(),
            SitePage.failure_count.asc(),
            SitePage.id.asc(),
        )
        .limit(limit)
        .with_for_update(skip_locked=True, of=SitePage)
    )


class SqlCrawlSchedulerRepository:
    """PostgreSQL claim/lease repository with stale-worker fencing."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = _now,
        frontier_policy: FrontierPolicy | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._frontier_policy = frontier_policy or FrontierPolicy()

    def claim_due(
        self,
        *,
        owner: str,
        limit: int,
        lease_duration: timedelta,
        now: datetime | None = None,
        urls: tuple[str, ...] = (),
    ) -> tuple[CrawlClaim, ...]:
        self._validate_owner(owner)
        if limit <= 0:
            raise ValueError("claim 數量必須大於零")
        self._validate_duration(lease_duration, "lease")
        checked_at = now or self._clock()
        expires_at = checked_at + lease_duration
        with self._factory.begin() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                due = (
                    due_pages_statement(
                        now=checked_at,
                        limit=limit,
                        frontier_policy=self._frontier_policy,
                        urls=urls,
                    )
                    .with_only_columns(SitePage.id)
                    .cte("due_pages")
                    .prefix_with("MATERIALIZED")
                )
                page_token = sql_cast(
                    func.md5(
                        SitePage.id.cast(String) + func.clock_timestamp().cast(String)
                    ),
                    SitePage.crawl_lease_token.type,
                )
                rows = (
                    session.execute(
                        update(SitePage)
                        .where(
                            SitePage.id == due.c.id,
                            or_(
                                SitePage.crawl_lease_expires_at.is_(None),
                                SitePage.crawl_lease_expires_at <= checked_at,
                            ),
                            SitePage.next_crawl_at <= checked_at,
                        )
                        .values(
                            crawl_lease_owner=owner,
                            crawl_lease_token=page_token,
                            crawl_lease_expires_at=expires_at,
                            crawl_status=SiteCrawlStatus.FETCHING.value,
                            last_scheduled_at=checked_at,
                        )
                        .returning(
                            SitePage.id.label("page_id"),
                            SitePage.crawl_lease_token.label("lease_token"),
                            SitePage.canonical_url.label("canonical_url"),
                            SitePage.host.label("host"),
                            SitePage.page_type.label("page_type"),
                            SitePage.unit.label("unit"),
                            SitePage.minimum_depth.label("minimum_depth"),
                            SitePage.etag.label("etag"),
                            SitePage.last_modified.label("last_modified"),
                            SitePage.content_hash.label("content_hash"),
                            SitePage.title.label("title"),
                            SitePage.crawl_priority.label("crawl_priority"),
                            SitePage.next_crawl_at.label("next_crawl_at"),
                            SitePage.failure_count.label("failure_count"),
                            SitePage.changed_streak.label("changed_streak"),
                            SitePage.unchanged_streak.label("unchanged_streak"),
                            SitePage.last_retry_after_at.label("last_retry_after_at"),
                        )
                    )
                    .mappings()
                    .all()
                )
                claims = []
                attempts = []
                for row in rows:
                    page_id = row["page_id"]
                    token = row["lease_token"]
                    claims.append(
                        CrawlClaim(
                            page_id=page_id,
                            canonical_url=row["canonical_url"],
                            owner=owner,
                            token=token,
                            lease_expires_at=expires_at,
                            crawl_priority=row["crawl_priority"],
                            next_crawl_at=row["next_crawl_at"],
                            failure_count=row["failure_count"],
                            host=row["host"],
                            page_type=row["page_type"],
                            unit=row["unit"],
                            minimum_depth=row["minimum_depth"],
                            etag=row["etag"],
                            last_modified=row["last_modified"],
                            content_hash=row["content_hash"],
                            title=row["title"],
                            changed_streak=row["changed_streak"],
                            unchanged_streak=row["unchanged_streak"],
                            last_retry_after_at=row["last_retry_after_at"],
                        )
                    )
                    attempts.append(
                        {
                            "site_page_id": page_id,
                            "lease_token": token,
                            "worker_id": owner,
                            "outcome": "running",
                        }
                    )
                if attempts:
                    session.execute(insert(SiteCrawlAttempt), attempts)
                return tuple(claims)

            # SQLite test fallback.  Production PostgreSQL takes the single
            # UPDATE ... RETURNING path above.
            claims: list[CrawlClaim] = []
            attempts: list[dict[str, object]] = []
            pages = session.scalars(
                due_pages_statement(
                    now=checked_at,
                    limit=limit,
                    frontier_policy=self._frontier_policy,
                    urls=urls,
                )
            ).all()
            for page in pages:
                token = uuid.uuid4()
                page.crawl_lease_owner = owner
                page.crawl_lease_token = token
                page.crawl_lease_expires_at = expires_at
                page.crawl_status = SiteCrawlStatus.FETCHING.value
                page.last_scheduled_at = checked_at
                attempts.append(
                    {
                        "site_page_id": page.id,
                        "lease_token": token,
                        "worker_id": owner,
                        "outcome": "running",
                    }
                )
                claims.append(
                    CrawlClaim(
                        page_id=page.id,
                        canonical_url=page.canonical_url,
                        owner=owner,
                        token=token,
                        lease_expires_at=expires_at,
                        crawl_priority=page.crawl_priority,
                        next_crawl_at=page.next_crawl_at,
                        failure_count=page.failure_count,
                        host=page.host,
                        page_type=page.page_type,
                        unit=page.unit,
                        minimum_depth=page.minimum_depth,
                        etag=page.etag,
                        last_modified=page.last_modified,
                        content_hash=page.content_hash,
                        title=page.title,
                        changed_streak=page.changed_streak,
                        unchanged_streak=page.unchanged_streak,
                        last_retry_after_at=page.last_retry_after_at,
                    )
                )
            if attempts:
                session.execute(insert(SiteCrawlAttempt), attempts)
        return tuple(claims)

    def renew(
        self,
        claim: CrawlClaim,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        self._validate_duration(lease_duration, "lease")
        checked_at = now or self._clock()
        with self._factory.begin() as session:
            page = self._owned_lease(session, claim, checked_at)
            if page is None:
                self._mark_lease_lost(session, claim, checked_at)
                return False
            page.crawl_lease_expires_at = checked_at + lease_duration
            return True

    def complete(
        self,
        claim: CrawlClaim,
        *,
        crawl_status: str,
        next_crawl_at: datetime,
        now: datetime | None = None,
        http_status: int | None = None,
        content_changed: bool | None = None,
        links_discovered: int = 0,
        ingestion_performed: bool = False,
        outcome: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        content_type: str | None = None,
        content_length: int | None = None,
        final_url: str | None = None,
    ) -> bool:
        checked_at = now or self._clock()
        with self._factory.begin() as session:
            page = self._owned_lease(session, claim, checked_at)
            if page is None:
                self._mark_lease_lost(session, claim, checked_at)
                return False
            page.crawl_status = crawl_status
            page.last_crawled_at = checked_at
            page.last_successful_crawl_at = checked_at
            page.failure_count = 0
            page.next_crawl_at = next_crawl_at
            page.http_status = http_status
            if etag is not None:
                page.etag = etag
            if last_modified is not None:
                page.last_modified = last_modified
            if content_changed is True:
                page.changed_streak += 1
                page.unchanged_streak = 0
            elif content_changed is False:
                page.unchanged_streak += 1
                page.changed_streak = 0
            self._finish_attempt(
                session,
                claim,
                outcome=outcome
                or (
                    "success_changed"
                    if content_changed
                    else "not_modified"
                    if http_status == 304
                    else "success_unchanged"
                ),
                finished_at=checked_at,
                http_status=http_status,
                content_changed=content_changed,
                links_discovered=links_discovered,
                ingestion_performed=ingestion_performed,
                content_type=content_type,
                content_length=content_length,
                final_url=final_url,
                etag=etag,
                last_modified=last_modified,
            )
            self._clear_lease(page)
            return True

    def fail(
        self,
        claim: CrawlClaim,
        *,
        decision: FailureDecision,
        http_status: int | None,
        now: datetime | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
        retry_after: str | None = None,
        content_type: str | None = None,
        content_length: int | None = None,
        final_url: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        ingestion_performed: bool = False,
    ) -> bool:
        checked_at = now or self._clock()
        with self._factory.begin() as session:
            page = self._owned_lease(session, claim, checked_at)
            if page is None:
                self._mark_lease_lost(session, claim, checked_at)
                return False
            page.crawl_status = decision.crawl_status
            page.http_status = http_status
            page.last_crawled_at = checked_at
            page.failure_count += 1
            page.next_crawl_at = decision.next_crawl_at
            page.last_error_kind = error_kind or decision.reason[:100]
            page.last_error_at = checked_at
            if retry_after is not None:
                page.last_retry_after_at = checked_at
            if decision.deactivate:
                page.is_active = False
            self._finish_attempt(
                session,
                claim,
                outcome=(
                    "blocked"
                    if decision.crawl_status == SiteCrawlStatus.BLOCKED.value
                    else "excluded"
                    if decision.deactivate
                    else "failed_transient"
                ),
                finished_at=checked_at,
                http_status=http_status,
                error_kind=error_kind,
                error_message=error_message or decision.reason,
                content_type=content_type,
                content_length=content_length,
                final_url=final_url,
                etag=etag,
                last_modified=last_modified,
                ingestion_performed=ingestion_performed,
            )
            self._clear_lease(page)
        return True

    def schedule_pages(
        self,
        *,
        urls: tuple[str, ...] = (),
        unit: str | None = None,
        host: str | None = None,
        page_type: str | None = None,
        run_at: datetime | None = None,
    ) -> int:
        checked_at = run_at or self._clock()
        conditions: list[ColumnElement[bool]] = [SitePage.is_active.is_(True)]
        if urls:
            conditions.append(SitePage.canonical_url.in_(urls))
        if unit:
            conditions.append(SitePage.unit == unit)
        if host:
            conditions.append(SitePage.host == host.casefold().rstrip("."))
        if page_type:
            conditions.append(SitePage.page_type == page_type)
        with self._factory.begin() as session:
            pages = session.scalars(
                select(SitePage).where(*conditions).with_for_update()
            ).all()
            scheduled = 0
            for page in pages:
                if not page.is_indexable or not is_crawlable_page_path(page.path):
                    continue
                if page.next_crawl_at is None or checked_at < page.next_crawl_at:
                    page.next_crawl_at = checked_at
                page.last_scheduled_at = checked_at
                if page.crawl_status not in {
                    SiteCrawlStatus.BLOCKED.value,
                    SiteCrawlStatus.EXCLUDED.value,
                }:
                    page.crawl_status = SiteCrawlStatus.QUEUED.value
                scheduled += 1
            return scheduled

    def status(self, *, now: datetime | None = None) -> dict[str, object]:
        checked_at = now or self._clock()
        with self._factory.begin() as session:
            eligible = (
                SitePage.is_active.is_(True),
                SitePage.is_indexable.is_(True),
                SitePage.crawl_status.not_in(
                    [
                        SiteCrawlStatus.BLOCKED.value,
                        SiteCrawlStatus.EXCLUDED.value,
                    ]
                ),
            )
            lease_available = or_(
                SitePage.crawl_lease_expires_at.is_(None),
                SitePage.crawl_lease_expires_at <= checked_at,
            )
            due = (
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(
                        *eligible,
                        lease_available,
                        SitePage.next_crawl_at.is_not(None),
                        SitePage.next_crawl_at <= checked_at,
                    )
                )
                or 0
            )
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(*eligible, lease_available)
                )
                or 0
            )
            leased = (
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(SitePage.crawl_lease_expires_at > checked_at)
                )
                or 0
            )
            active_workers = (
                session.scalar(
                    select(func.count(func.distinct(SitePage.crawl_lease_owner)))
                    .select_from(SitePage)
                    .where(
                        SitePage.crawl_lease_owner.is_not(None),
                        SitePage.crawl_lease_expires_at > checked_at,
                    )
                )
                or 0
            )
            failed = (
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(SitePage.crawl_status == SiteCrawlStatus.FAILED.value)
                )
                or 0
            )
            blocked = (
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(SitePage.crawl_status == SiteCrawlStatus.BLOCKED.value)
                )
                or 0
            )
            pending_ingestion = (
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(
                        SitePage.content_hash.is_not(None),
                        or_(
                            SitePage.ingestion_status.in_(["pending", "failed"]),
                            SitePage.announcement_ingestion_status.in_(
                                ["pending", "failed"]
                            ),
                        ),
                    )
                )
                or 0
            )
            incomplete_ingestion = (
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(
                        SitePage.content_hash.is_not(None),
                        SitePage.announcement_ingestion_status == "incomplete",
                    )
                )
                or 0
            )
            next_due = session.scalar(
                select(func.min(SitePage.next_crawl_at)).where(
                    *eligible,
                    lease_available,
                )
            )
            recent_attempt_rows = session.execute(
                select(SiteCrawlAttempt.outcome, func.count())
                .where(SiteCrawlAttempt.started_at >= checked_at - timedelta(hours=24))
                .group_by(SiteCrawlAttempt.outcome)
            ).all()
            return {
                "due": int(due),
                "pending": int(pending),
                "leased": int(leased),
                "failed": int(failed),
                "blocked": int(blocked),
                "pending_ingestion": int(pending_ingestion),
                "incomplete_ingestion": int(incomplete_ingestion),
                "next_due_at": next_due,
                "active_workers": int(active_workers),
                "recent_attempts": {
                    outcome: int(count) for outcome, count in recent_attempt_rows
                },
            }

    def _owned_lease(
        self,
        session: Session,
        claim: CrawlClaim,
        now: datetime,
    ) -> SitePage | None:
        # Every mutating path locks the page row. Claim does the same, so the
        # owner/token/expiry predicate fences stale workers atomically.
        return session.scalar(
            select(SitePage)
            .where(
                SitePage.id == claim.page_id,
                SitePage.crawl_lease_owner == claim.owner,
                SitePage.crawl_lease_token == claim.token,
                SitePage.crawl_lease_expires_at > now,
            )
            .with_for_update(of=SitePage)
        )

    @staticmethod
    def _clear_lease(page: SitePage) -> None:
        page.crawl_lease_owner = None
        page.crawl_lease_token = None
        page.crawl_lease_expires_at = None

    @staticmethod
    def _finish_attempt(
        session: Session,
        claim: CrawlClaim,
        *,
        outcome: str,
        finished_at: datetime,
        http_status: int | None = None,
        content_changed: bool | None = None,
        links_discovered: int = 0,
        ingestion_performed: bool = False,
        error_kind: str | None = None,
        error_message: str | None = None,
        content_type: str | None = None,
        content_length: int | None = None,
        final_url: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        attempt = session.scalar(
            select(SiteCrawlAttempt)
            .join(SitePage, SitePage.id == SiteCrawlAttempt.site_page_id)
            .where(
                SiteCrawlAttempt.site_page_id == claim.page_id,
                SiteCrawlAttempt.lease_token == claim.token,
                SitePage.crawl_lease_owner == claim.owner,
                SitePage.crawl_lease_token == claim.token,
                SitePage.crawl_lease_expires_at > finished_at,
            )
            .with_for_update()
        )
        if attempt is None:
            return
        attempt.finished_at = finished_at
        attempt.outcome = outcome
        attempt.http_status = http_status
        attempt.content_type = (content_type or "")[:255] or None
        attempt.content_length = (
            max(0, content_length) if content_length is not None else None
        )
        attempt.final_url = (final_url or "")[:2000] or None
        attempt.etag = (etag or "")[:500] or None
        attempt.last_modified = (last_modified or "")[:500] or None
        attempt.content_changed = content_changed
        attempt.links_discovered = max(0, links_discovered)
        attempt.ingestion_performed = ingestion_performed
        attempt.error_kind = error_kind
        attempt.error_message = (error_message or "")[:1000] or None
        attempt.duration_ms = max(
            0,
            int((finished_at - attempt.started_at).total_seconds() * 1000),
        )

    @staticmethod
    def _mark_lease_lost(
        session: Session,
        claim: CrawlClaim,
        finished_at: datetime,
    ) -> None:
        current_live_lease = (
            select(SitePage.id)
            .where(
                SitePage.id == claim.page_id,
                SitePage.crawl_lease_owner == claim.owner,
                SitePage.crawl_lease_token == claim.token,
                SitePage.crawl_lease_expires_at > finished_at,
            )
            .exists()
        )
        attempt = session.scalar(
            select(SiteCrawlAttempt)
            .where(
                SiteCrawlAttempt.site_page_id == claim.page_id,
                SiteCrawlAttempt.lease_token == claim.token,
                SiteCrawlAttempt.worker_id == claim.owner,
                SiteCrawlAttempt.outcome == "running",
                # The attempt row is immutable lease identity.  Allow its
                # terminal tombstone only when that exact page/owner/token
                # lease is no longer live (expired or replaced), so a new
                # lease cannot be touched by the stale worker.
                not_(current_live_lease),
            )
            .with_for_update()
        )
        if attempt is None:
            return
        attempt.finished_at = finished_at
        attempt.outcome = "lease_lost"
        attempt.error_kind = "lease_lost"
        attempt.error_message = "worker lease 已被其他 worker 取代"
        attempt.duration_ms = max(
            0,
            int((finished_at - attempt.started_at).total_seconds() * 1000),
        )

    @staticmethod
    def _validate_owner(owner: str) -> None:
        if not owner or not owner.strip() or len(owner) > 200:
            raise ValueError("lease owner 不得為空且長度不可超過 200")

    @staticmethod
    def _validate_duration(duration: timedelta, name: str) -> None:
        if duration <= timedelta(0):
            raise ValueError(f"{name} duration 必須大於零")


def is_crawlable_page_path(path: str) -> bool:
    return not any(
        path.casefold().endswith(suffix) for suffix in NON_HTML_RESOURCE_SUFFIXES
    )
