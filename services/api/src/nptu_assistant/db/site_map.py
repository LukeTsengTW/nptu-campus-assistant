from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, cast
from urllib.parse import urlsplit

from sqlalchemy import (
    Float,
    String,
    and_,
    case,
    column,
    false,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    true,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session, selectinload, sessionmaker

from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_nptu_url
from nptu_assistant.crawlers.crawl_policy import is_crawlable_url
from nptu_assistant.crawlers.official_units import DocumentSearchScope
from nptu_assistant.crawlers.site_map import (
    DISCOVERY_SOURCE_PRIORITY,
    PAGE_TYPE_PRIORITY,
    SiteCrawlStatus,
    SiteDiscoverySource,
    FrontierPolicy,
    SiteLinkType,
    SiteLinkUpsert,
    SiteMapBatchWriteResult,
    SiteMapCandidate,
    SiteMapQueryTimeout,
    SiteMapRepository,
    SiteMapSyncSummary,
    SiteMapWriteResult,
    SitePageType,
    SitePageUpsert,
    source_priority,
)
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchPlan,
)
from nptu_assistant.db.models import Announcement, Document, SiteLink, SitePage, Source

TITLE_TRIGRAM_THRESHOLD = 0.18
PATH_TRIGRAM_THRESHOLD = 0.22
ANCHOR_TRIGRAM_THRESHOLD = 0.10
PG_TRGM_PREFILTER_THRESHOLD = min(
    TITLE_TRIGRAM_THRESHOLD,
    PATH_TRIGRAM_THRESHOLD,
    ANCHOR_TRIGRAM_THRESHOLD,
)


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
        site_map_query_budget_ratio: float = 0.25,
        site_map_query_min_seconds: float = 0.05,
        site_map_query_max_seconds: float = 0.75,
        frontier_policy: FrontierPolicy | None = None,
    ) -> None:
        if not 0 < site_map_query_budget_ratio <= 1:
            raise ValueError("site map 查詢 budget ratio 必須介於 0 與 1 之間")
        if site_map_query_min_seconds <= 0:
            raise ValueError("site map 查詢最小時間必須大於零")
        if site_map_query_max_seconds < site_map_query_min_seconds:
            raise ValueError("site map 查詢最大時間不得小於最小時間")
        self._factory = factory
        self._clock = clock
        self._site_map_query_budget_ratio = site_map_query_budget_ratio
        self._site_map_query_min_seconds = site_map_query_min_seconds
        self._site_map_query_max_seconds = site_map_query_max_seconds
        self._frontier_policy = frontier_policy or FrontierPolicy()

    def upsert_page(self, page: SitePageUpsert) -> SiteMapWriteResult:
        now = self._clock()
        with self._factory.begin() as session:
            outcome = self._upsert_pages_in_session(session, (page,), now=now)[0]
            return SiteMapWriteResult(created=outcome[1], updated=outcome[2])

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
            self._upsert_pages_in_session(session, (source, target), now=now)
            page_ids = self._page_ids_in_session(
                session, (source.canonical_url, target.canonical_url)
            )
            link_outcomes = self._upsert_links_in_session(
                session,
                (
                    (
                        page_ids[source.canonical_url],
                        page_ids[target.canonical_url],
                        anchor_text,
                        link_type,
                    ),
                ),
                now=now,
            )
            created = bool(link_outcomes and link_outcomes[0])
            return SiteMapWriteResult(created=created, updated=not created)

    def persist_fetched_page(
        self,
        source: SitePageUpsert,
        *,
        title: str | None,
        content_hash: str,
        http_status: int | None,
        etag: str | None = None,
        last_modified: str | None = None,
        links: Sequence[SiteLinkUpsert] = (),
        page_id: uuid.UUID | None = None,
        lease_owner: str | None = None,
        lease_token: uuid.UUID | None = None,
        lease_expires_at: datetime | None = None,
        allow_unleased: bool = False,
    ) -> SiteMapBatchWriteResult:
        """在單一 transaction 批次寫入 source、targets、edges、crawl state。"""
        now = self._clock()
        with self._factory.begin() as session:
            source_outcome = (
                self._upsert_pages_in_session(session, (source,), now=now)[0]
                if page_id is None
                else (source, False, False)
            )
            source_page = session.scalar(
                select(SitePage)
                .where(
                    SitePage.canonical_url == source.canonical_url,
                    *((SitePage.id == page_id,) if page_id is not None else ()),
                )
                .with_for_update()
            )
            if source_page is None:
                raise RuntimeError("site map fetched source page 建立失敗")
            if page_id is not None and (
                lease_owner is None or lease_token is None or lease_expires_at is None
            ):
                raise RuntimeError("site map fetched page lease context incomplete")
            if page_id is None and any(
                value is not None
                for value in (lease_owner, lease_token, lease_expires_at)
            ):
                raise RuntimeError(
                    "site map fetched page lease context requires page id"
                )
            if page_id is None and not allow_unleased:
                raise RuntimeError(
                    "site map fetched page 必須有 lease；live discovery fallback "
                    "需明確設定 allow_unleased"
                )
            if lease_token is not None and (
                source_page.crawl_lease_token != lease_token
                or source_page.crawl_lease_owner != lease_owner
                or source_page.crawl_lease_expires_at is None
                or source_page.crawl_lease_expires_at <= now
                or (lease_expires_at is not None and lease_expires_at <= now)
            ):
                raise RuntimeError("site map page lease 已失效，拒絕寫入")
            self._apply_crawl_success(
                source_page,
                title=title,
                content_hash=content_hash,
                http_status=http_status,
                etag=etag,
                last_modified=last_modified,
                now=now,
            )
            bounded_links, frontier_skipped = self._bounded_frontier_links_in_session(
                session,
                links,
                now=now,
            )
            targets: dict[str, SitePageUpsert] = {}
            for link in bounded_links:
                if link.target.canonical_url != source.canonical_url:
                    targets.setdefault(link.target.canonical_url, link.target)
            target_pages = tuple(targets.values())
            target_outcomes = self._upsert_pages_in_session(
                session, target_pages, now=now
            )
            page_ids = self._page_ids_in_session(
                session,
                (source.canonical_url, *targets),
            )
            edge_values = tuple(
                (
                    page_ids[source.canonical_url],
                    page_ids[link.target.canonical_url],
                    link.anchor_text,
                    link.link_type,
                )
                for link in bounded_links
                if link.target.canonical_url in page_ids
            )
            edge_outcomes = self._upsert_links_in_session(session, edge_values, now=now)
            return SiteMapBatchWriteResult(
                source_created=source_outcome[1],
                source_updated=source_outcome[2],
                target_created=sum(1 for item in target_outcomes if item[1]),
                target_updated=sum(1 for item in target_outcomes if item[2]),
                links_created=sum(1 for item in edge_outcomes if item),
                links_updated=sum(1 for item in edge_outcomes if not item),
                # PostgreSQL uses one set-based lock statement and one
                # set-based state lookup. Live acceptance traces SQL; this
                # field is the fixed operation budget exposed to callers.
                statement_count=7,
                frontier_skipped=frontier_skipped,
            )

    def _bounded_frontier_links_in_session(
        self,
        session: Session,
        links: Sequence[SiteLinkUpsert],
        *,
        now: datetime,
    ) -> tuple[tuple[SiteLinkUpsert, ...], int]:
        """Filter and cap newly discovered links inside one DB transaction."""

        unique: dict[str, SiteLinkUpsert] = {}
        for link in links:
            normalized = self._frontier_policy.normalize_target(
                link.target.canonical_url,
                allowed_hosts=None,
                allow_document=True,
            )
            if normalized is None:
                continue
            target = replace(
                link.target,
                canonical_url=normalized,
                is_indexable=link.target.is_indexable and is_crawlable_url(normalized),
                next_crawl_at=(
                    link.target.next_crawl_at
                    or self._frontier_policy.new_target_due_at(now)
                ),
            )
            previous = unique.get(normalized)
            if previous is None or (not previous.anchor_text and link.anchor_text):
                unique[normalized] = replace(link, target=target)
        if not unique:
            return (), 0

        hosts = sorted(
            {(urlsplit(url).hostname or "").casefold().rstrip(".") for url in unique}
        )
        is_postgresql = (
            session.bind is not None and session.bind.dialect.name == "postgresql"
        )
        if is_postgresql:
            # All writers use the same global-then-host order.  One statement
            # acquires every lock, so host count cannot turn this transaction
            # into an advisory-lock N+1 sequence.
            session.execute(
                text(
                    "WITH lock_keys AS ("
                    " SELECT 0 AS lock_group, 'global' AS lock_name, "
                    " hashtextextended('nptu-frontier-global-cap', 0)::bigint "
                    " AS lock_key"
                    " UNION ALL "
                    " SELECT 1, host, hashtext(host)::bigint"
                    " FROM unnest(CAST(:hosts AS text[])) AS requested(host)"
                    ")"
                    " SELECT pg_advisory_xact_lock(lock_key)"
                    " FROM lock_keys"
                    " ORDER BY lock_group, lock_name"
                ),
                {"hosts": list(hosts)},
            )

            # Existing URLs, the global pending count, and all requested host
            # counts are read under the locks in one bounded SQL statement.
            state_rows = session.execute(
                text(
                    "WITH requested_urls AS ("
                    " SELECT unnest(CAST(:urls AS text[])) AS canonical_url"
                    "), requested_hosts AS ("
                    " SELECT unnest(CAST(:hosts AS text[])) AS host"
                    "), eligible AS ("
                    " SELECT sp.host, count(*)::bigint AS pending_count"
                    " FROM site_pages sp"
                    " JOIN requested_hosts rh ON rh.host = sp.host"
                    " WHERE sp.is_active IS TRUE"
                    "   AND sp.is_indexable IS TRUE"
                    "   AND sp.crawl_status NOT IN ('blocked', 'excluded')"
                    "   AND sp.crawl_status IN "
                    "       ('discovered', 'queued', 'failed', 'fetching')"
                    " GROUP BY sp.host"
                    "), total AS ("
                    " SELECT count(*)::bigint AS pending_count"
                    " FROM site_pages sp"
                    " WHERE sp.is_active IS TRUE"
                    "   AND sp.is_indexable IS TRUE"
                    "   AND sp.crawl_status NOT IN ('blocked', 'excluded')"
                    "   AND sp.crawl_status IN "
                    "       ('discovered', 'queued', 'failed', 'fetching')"
                    ")"
                    " SELECT 'existing' AS row_kind, sp.canonical_url, "
                    "        sp.host, NULL::bigint AS pending_count"
                    " FROM site_pages sp"
                    " JOIN requested_urls ru ON ru.canonical_url = sp.canonical_url"
                    " UNION ALL"
                    " SELECT 'host', NULL, host, pending_count FROM eligible"
                    " UNION ALL"
                    " SELECT 'total', NULL, NULL, pending_count FROM total"
                ),
                {"urls": list(unique), "hosts": list(hosts)},
            ).mappings()
            existing_urls: set[str] = set()
            pending_by_host: dict[str, int] = {host: 0 for host in hosts}
            pending_total = 0
            for row in state_rows:
                kind = row["row_kind"]
                if kind == "existing":
                    existing_urls.add(str(row["canonical_url"]))
                elif kind == "host":
                    pending_by_host[str(row["host"])] = int(row["pending_count"] or 0)
                else:
                    pending_total = int(row["pending_count"] or 0)
        else:
            existing_rows = session.execute(
                select(SitePage.canonical_url).where(
                    SitePage.canonical_url.in_(tuple(unique))
                )
            ).scalars()
            existing_urls = set(existing_rows)
            pending_total = int(
                session.scalar(
                    select(func.count())
                    .select_from(SitePage)
                    .where(
                        SitePage.is_active.is_(True),
                        SitePage.is_indexable.is_(True),
                        SitePage.crawl_status.not_in(
                            [
                                SiteCrawlStatus.BLOCKED.value,
                                SiteCrawlStatus.EXCLUDED.value,
                            ]
                        ),
                        SitePage.crawl_status.in_(
                            [
                                SiteCrawlStatus.DISCOVERED.value,
                                SiteCrawlStatus.QUEUED.value,
                                SiteCrawlStatus.FAILED.value,
                                SiteCrawlStatus.FETCHING.value,
                            ]
                        ),
                    )
                )
                or 0
            )
            pending_by_host = {
                host: int(
                    session.scalar(
                        select(func.count())
                        .select_from(SitePage)
                        .where(
                            SitePage.host == host,
                            SitePage.is_active.is_(True),
                            SitePage.is_indexable.is_(True),
                            SitePage.crawl_status.not_in(
                                [
                                    SiteCrawlStatus.BLOCKED.value,
                                    SiteCrawlStatus.EXCLUDED.value,
                                ]
                            ),
                            SitePage.crawl_status.in_(
                                [
                                    SiteCrawlStatus.DISCOVERED.value,
                                    SiteCrawlStatus.QUEUED.value,
                                    SiteCrawlStatus.FAILED.value,
                                    SiteCrawlStatus.FETCHING.value,
                                ]
                            ),
                        )
                    )
                    or 0
                )
                for host in hosts
            }
        kept: list[SiteLinkUpsert] = []
        skipped = 0
        for link in unique.values():
            # Keep the edge for graph metadata, but resource targets are
            # explicitly excluded and never consume the HTML frontier.
            if not link.target.is_indexable:
                kept.append(link)
                continue
            if link.target.canonical_url in existing_urls:
                kept.append(link)
                continue
            host = (urlsplit(link.target.canonical_url).hostname or "").casefold()
            if pending_total >= self._frontier_policy.max_pending_total:
                skipped += 1
                continue
            if (
                pending_by_host.get(host, 0)
                >= self._frontier_policy.per_host_pending_cap
            ):
                skipped += 1
                continue
            kept.append(link)
            pending_total += 1
            pending_by_host[host] = pending_by_host.get(host, 0) + 1
        return tuple(kept), skipped

    def record_crawl_success(
        self,
        canonical_url: str,
        *,
        title: str | None,
        content_hash: str,
        http_status: int | None,
        etag: str | None = None,
        last_modified: str | None = None,
        allow_unleased: bool = False,
    ) -> SiteMapWriteResult:
        if not allow_unleased:
            raise RuntimeError(
                "site map crawl success 必須有 lease；live fallback "
                "需明確設定 allow_unleased"
            )
        now = self._clock()
        with self._factory.begin() as session:
            outcome = self._upsert_pages_in_session(
                session,
                (SitePageUpsert(canonical_url=canonical_url),),
                now=now,
            )[0]
            page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == canonical_url)
            )
            if page is None:
                raise RuntimeError("site map crawl success page 建立失敗")
            self._apply_crawl_success(
                page,
                title=title,
                content_hash=content_hash,
                http_status=http_status,
                etag=etag,
                last_modified=last_modified,
                now=now,
            )
            return SiteMapWriteResult(created=outcome[1], updated=outcome[2])

    def record_crawl_failure(
        self,
        canonical_url: str,
        *,
        http_status: int | None = None,
        status: SiteCrawlStatus = SiteCrawlStatus.FAILED,
        allow_unleased: bool = False,
    ) -> SiteMapWriteResult:
        if not allow_unleased:
            raise RuntimeError(
                "site map crawl failure 必須有 lease；live fallback "
                "需明確設定 allow_unleased"
            )
        now = self._clock()
        with self._factory.begin() as session:
            outcome = self._upsert_pages_in_session(
                session,
                (SitePageUpsert(canonical_url=canonical_url),),
                now=now,
            )[0]
            page = session.scalar(
                select(SitePage).where(SitePage.canonical_url == canonical_url)
            )
            if page is None:
                raise RuntimeError("site map crawl failure page 建立失敗")
            page.crawl_status = status.value
            page.http_status = http_status
            page.last_crawled_at = now
            page.failure_count += 1
            return SiteMapWriteResult(created=outcome[1], updated=outcome[2])

    def find_candidates(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope | None,
        allowed_hosts: Collection[str],
        limit: int,
        deadline: SearchDeadline | None = None,
    ) -> tuple[SiteMapCandidate, ...]:
        if limit <= 0 or not allowed_hosts:
            return ()
        if deadline is not None:
            deadline.raise_if_expired()
        with self._factory.begin() as session:
            is_postgresql = (
                session.bind is not None and session.bind.dialect.name == "postgresql"
            )
            if is_postgresql:
                config_sql = (
                    "SELECT set_config('statement_timeout', :timeout, true), "
                    "set_config('pg_trgm.similarity_threshold', "
                    ":trigram_threshold, true)"
                )
                config_parameters: dict[str, object] = {
                    "trigram_threshold": str(PG_TRGM_PREFILTER_THRESHOLD),
                    "timeout": f"{math.ceil(self._site_map_query_max_seconds * 1000)}ms",
                }
                if deadline is not None:
                    budget_seconds = self._site_map_budget_seconds(deadline)
                    timeout_ms = max(1, math.ceil(budget_seconds * 1000))
                    config_parameters["timeout"] = f"{timeout_ms}ms"
                    deadline.raise_if_expired()
                session.execute(
                    text(config_sql),
                    config_parameters,
                )
                if deadline is not None:
                    deadline.raise_if_expired()
            query = self.build_candidate_query(
                plan,
                scope=scope,
                allowed_hosts=allowed_hosts,
                limit=limit,
                dialect_name="postgresql" if is_postgresql else "sqlite",
            )
            try:
                rows = session.execute(query).all()
            except Exception as exc:
                error_text = str(exc).casefold()
                if deadline is not None and deadline.expired():
                    raise SearchDeadlineExceeded(
                        "共同搜尋 deadline 已在 site map SQL 期間耗盡"
                    ) from exc
                if is_postgresql and any(
                    marker in error_text
                    for marker in ("statement timeout", "canceling statement")
                ):
                    raise SiteMapQueryTimeout(
                        "site map candidate SQL 子預算已逾時"
                    ) from exc
                raise
            if deadline is not None:
                deadline.raise_if_expired()
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
                lexical_relevance=float(row[1] or 0.0),
                structural_score=float(row[2] or 0.0),
                final_score=float(row[3] or 0.0),
                is_crawlable=is_crawlable_url(row[0].canonical_url),
            )
            for row in rows
        )

    def build_candidate_query(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope | None,
        allowed_hosts: Collection[str],
        limit: int,
        dialect_name: str = "postgresql",
    ) -> Any:
        """Build the exact candidate statement used by ``find_candidates``.

        Keeping this statement construction reusable lets PostgreSQL acceptance
        tests run EXPLAIN on the production query shape instead of a simplified
        surrogate.
        """

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
        intent = " ".join(
            (plan.query, *plan.retrieval_queries[1:3], *plan.concepts)
        ).casefold()
        announcement_intent = any(
            token in intent for token in ("公告", "通知", "訊息", "最新消息")
        )
        document_intent = any(
            token in intent for token in ("文件", "辦法", "表單", "規章", "申請表")
        )
        homepage_intent = any(token in intent for token in ("首頁", "主頁", "home"))
        page_type_score = case(
            (
                and_(
                    literal(announcement_intent),
                    SitePage.page_type == SitePageType.ANNOUNCEMENT_LISTING.value,
                ),
                0.34,
            ),
            (
                and_(
                    literal(announcement_intent),
                    SitePage.page_type == SitePageType.ANNOUNCEMENT_DETAIL.value,
                ),
                0.28,
            ),
            (
                and_(
                    literal(document_intent),
                    SitePage.page_type == SitePageType.OFFICIAL_DOCUMENT.value,
                ),
                0.34,
            ),
            (
                and_(
                    literal(homepage_intent),
                    SitePage.page_type == SitePageType.UNIT_HOMEPAGE.value,
                ),
                0.25,
            ),
            else_=0.0,
        )
        query_terms = self._query_terms(plan)
        page_lexical_scores: Any = None
        anchor_lexical_scores: Any = None
        candidate_filters = (
            SitePage.is_active.is_(True),
            SitePage.is_indexable.is_(True),
            SitePage.crawl_status.not_in(
                [
                    SiteCrawlStatus.BLOCKED.value,
                    SiteCrawlStatus.EXCLUDED.value,
                ]
            ),
            _host_scope(allowed_hosts),
        )
        if dialect_name == "postgresql":
            (
                lexical_relevance,
                anchor_relevance,
                lexical_match,
                page_lexical_scores,
                anchor_lexical_scores,
            ) = self._postgres_lexical_expressions(
                query_terms,
                candidate_filters=candidate_filters,
            )
        else:
            lexical_relevance, anchor_relevance, lexical_match = (
                self._fallback_lexical_expressions(query_terms)
            )
        structural_score = func.greatest(
            literal(0.0),
            func.least(
                literal(1.0),
                case((unit_match, 0.35), else_=0.0)
                + case((preferred_match, 0.25), else_=0.0)
                + case(
                    (
                        and_(
                            literal(scope is not None and bool(scope.homepage_url)),
                            SitePage.canonical_url
                            == (scope.homepage_url if scope else ""),
                        ),
                        0.25,
                    ),
                    else_=0.0,
                )
                + page_type_score
                + func.least(SitePage.crawl_priority, 100) / 1_000.0
                + case(
                    (SitePage.last_successful_crawl_at.is_not(None), 0.05),
                    else_=0.0,
                )
                - func.least(SitePage.failure_count, 5) * 0.04,
            ),
        )
        final_score = lexical_relevance * 0.75 + structural_score * 0.25
        if dialect_name == "postgresql":
            candidate_scores = (
                select(
                    SitePage.id.label("page_id"),
                    lexical_relevance.label("lexical_relevance"),
                    structural_score.label("structural_score"),
                    anchor_relevance.label("anchor_relevance"),
                )
                .select_from(
                    SitePage.__table__.join(
                        page_lexical_scores,
                        page_lexical_scores.c.page_id == SitePage.id,
                    ).outerjoin(
                        anchor_lexical_scores,
                        anchor_lexical_scores.c.page_id == SitePage.id,
                    )
                )
                .where(
                    or_(
                        lexical_match,
                        SitePage.page_type.in_(
                            [
                                SitePageType.UNIT_HOMEPAGE.value,
                                SitePageType.ANNOUNCEMENT_LISTING.value,
                            ]
                        ),
                    )
                )
                .cte("site_map_candidate_scores")
                .prefix_with("MATERIALIZED")
            )
            candidate_final_score = (
                candidate_scores.c.lexical_relevance * 0.75
                + candidate_scores.c.structural_score * 0.25
            )
            return (
                select(
                    SitePage,
                    candidate_scores.c.lexical_relevance,
                    candidate_scores.c.structural_score,
                    candidate_final_score.label("final_score"),
                    candidate_scores.c.anchor_relevance,
                )
                .join(
                    candidate_scores,
                    candidate_scores.c.page_id == SitePage.id,
                )
                .order_by(
                    candidate_final_score.desc(),
                    candidate_scores.c.lexical_relevance.desc(),
                    candidate_scores.c.structural_score.desc(),
                    SitePage.minimum_depth.asc(),
                    SitePage.failure_count.asc(),
                    SitePage.last_successful_crawl_at.desc().nulls_last(),
                )
                .limit(limit)
            )
        return (
            select(
                SitePage,
                lexical_relevance.label("lexical_relevance"),
                structural_score.label("structural_score"),
                final_score.label("final_score"),
                anchor_relevance.label("anchor_relevance"),
            )
            .where(
                *candidate_filters,
                or_(
                    lexical_match,
                    SitePage.page_type.in_(
                        [
                            SitePageType.UNIT_HOMEPAGE.value,
                            SitePageType.ANNOUNCEMENT_LISTING.value,
                        ]
                    ),
                ),
            )
            .order_by(
                final_score.desc(),
                lexical_relevance.desc(),
                structural_score.desc(),
                SitePage.minimum_depth.asc(),
                SitePage.failure_count.asc(),
                SitePage.last_successful_crawl_at.desc().nulls_last(),
            )
            .limit(limit)
        )

    def _site_map_budget_seconds(self, deadline: SearchDeadline) -> float:
        remaining = deadline.remaining_seconds()
        bounded = min(
            self._site_map_query_max_seconds,
            remaining * self._site_map_query_budget_ratio,
        )
        return min(remaining, max(self._site_map_query_min_seconds, bounded))

    @staticmethod
    def _query_terms(plan: SearchPlan) -> tuple[tuple[str, float], ...]:
        terms: list[tuple[str, float]] = []
        seen: set[str] = set()
        weighted_values = [
            (plan.query, 1.0),
            *((item, 0.85) for item in plan.retrieval_queries[1:3]),
            *((item, 0.70) for item in plan.concepts),
        ]
        for value, weight in weighted_values:
            normalized = " ".join(value.split()).casefold()
            if normalized and normalized not in seen:
                terms.append((value[:200], weight))
                seen.add(normalized)
        return tuple(terms)

    @staticmethod
    def _postgres_lexical_expressions(
        query_terms: Sequence[tuple[str, float]],
        *,
        candidate_filters: Sequence[Any],
    ) -> tuple[Any, Any, Any, Any, Any]:
        term_values = (
            values(
                column("query_text", String),
                column("query_weight", Float),
                name="site_map_query_terms",
            )
            .data(query_terms)
            .cte("site_map_query_terms")
        )
        qtext = term_values.c.query_text
        qweight = term_values.c.query_weight
        contains_terms = tuple(term for term, _weight in query_terms)
        title_contains = (
            or_(*(SitePage.title.ilike(f"%{term}%") for term in contains_terms))
            if contains_terms
            else literal(False)
        )
        path_contains = (
            or_(*(SitePage.path.ilike(f"%{term}%") for term in contains_terms))
            if contains_terms
            else literal(False)
        )
        anchor_contains = (
            or_(*(SiteLink.anchor_text.ilike(f"%{term}%") for term in contains_terms))
            if contains_terms
            else literal(False)
        )
        # Keep the trigram prefilter in a dedicated candidate-id CTE.  This
        # lets PostgreSQL use the title/path/anchor GIN indexes before the
        # bounded similarity aggregation, while retaining anchor-only recall.
        page_term_candidates = (
            select(SitePage.id.label("page_id"))
            .select_from(SitePage.__table__.join(term_values, true()))
            .where(
                *candidate_filters,
                or_(
                    SitePage.title.op("%")(qtext),
                    SitePage.path.op("%")(qtext),
                    title_contains,
                    path_contains,
                ),
            )
        )
        anchor_term_candidates = (
            select(SiteLink.target_page_id.label("page_id"))
            .select_from(
                SiteLink.__table__.join(
                    SitePage, SiteLink.target_page_id == SitePage.id
                ).join(term_values, true())
            )
            .where(
                *candidate_filters,
                or_(SiteLink.anchor_text.op("%")(qtext), anchor_contains),
            )
        )
        candidate_page_ids = page_term_candidates.union(anchor_term_candidates).cte(
            "site_map_candidate_page_ids"
        )
        page_lexical_scores = (
            select(
                SitePage.id.label("page_id"),
                func.max(
                    func.similarity(func.coalesce(SitePage.title, ""), qtext) * qweight
                ).label("title_similarity"),
                func.max(
                    func.similarity(func.coalesce(SitePage.path, ""), qtext) * qweight
                ).label("path_similarity"),
                func.bool_or(
                    and_(
                        SitePage.title.op("%")(qtext),
                        func.similarity(func.coalesce(SitePage.title, ""), qtext)
                        >= TITLE_TRIGRAM_THRESHOLD,
                    )
                ).label("title_fuzzy_match"),
                func.bool_or(
                    and_(
                        SitePage.path.op("%")(qtext),
                        func.similarity(func.coalesce(SitePage.path, ""), qtext)
                        >= PATH_TRIGRAM_THRESHOLD,
                    )
                ).label("path_fuzzy_match"),
            )
            .select_from(
                SitePage.__table__.join(
                    candidate_page_ids, candidate_page_ids.c.page_id == SitePage.id
                ).join(term_values, true())
            )
            .where(*candidate_filters)
            .group_by(SitePage.id)
            .cte("site_map_page_lexical_scores")
        )
        anchor_lexical_scores = (
            select(
                SiteLink.target_page_id.label("page_id"),
                func.max(
                    func.similarity(func.coalesce(SiteLink.anchor_text, ""), qtext)
                    * qweight
                ).label("anchor_similarity"),
                func.bool_or(SiteLink.anchor_text.op("%")(qtext)).label("anchor_match"),
                func.bool_or(
                    and_(
                        SiteLink.anchor_text.op("%")(qtext),
                        func.similarity(SiteLink.anchor_text, qtext)
                        >= ANCHOR_TRIGRAM_THRESHOLD,
                    )
                ).label("anchor_fuzzy_match"),
            )
            .select_from(
                SiteLink.__table__.join(
                    candidate_page_ids,
                    candidate_page_ids.c.page_id == SiteLink.target_page_id,
                ).join(term_values, true())
            )
            .where(or_(SiteLink.anchor_text.op("%")(qtext), anchor_contains))
            .group_by(SiteLink.target_page_id)
            .cte("site_map_anchor_lexical_scores")
        )
        anchor_match = func.coalesce(
            anchor_lexical_scores.c.anchor_match,
            literal(False),
        )
        title_fuzzy_match = or_(
            page_lexical_scores.c.title_fuzzy_match,
            literal(False),
        )
        path_fuzzy_match = or_(
            page_lexical_scores.c.path_fuzzy_match,
            literal(False),
        )
        anchor_fuzzy_match = func.coalesce(
            anchor_lexical_scores.c.anchor_fuzzy_match,
            literal(False),
        )
        unit_contains = (
            or_(*(SitePage.unit.ilike(f"%{term}%") for term in contains_terms))
            if contains_terms
            else literal(False)
        )
        lexical = func.least(
            literal(1.0),
            func.greatest(
                func.coalesce(page_lexical_scores.c.title_similarity, 0.0),
                func.coalesce(page_lexical_scores.c.path_similarity, 0.0),
                func.coalesce(anchor_lexical_scores.c.anchor_similarity, 0.0),
            )
            * 0.65
            + case((title_contains, 0.28), else_=0.0)
            + case((path_contains, 0.12), else_=0.0)
            + case((anchor_match, 0.10), else_=0.0)
            + case((unit_contains, 0.12), else_=0.0),
        )
        return (
            lexical,
            func.coalesce(anchor_lexical_scores.c.anchor_similarity, 0.0),
            or_(
                title_contains,
                path_contains,
                anchor_match,
                title_fuzzy_match,
                path_fuzzy_match,
                anchor_fuzzy_match,
                unit_contains,
            ),
            page_lexical_scores,
            anchor_lexical_scores,
        )

    @staticmethod
    def _fallback_lexical_expressions(
        query_terms: Sequence[tuple[str, float]],
    ) -> tuple[Any, Any, Any]:
        terms = tuple(term for term, _weight in query_terms)
        title_contains = or_(*(SitePage.title.ilike(f"%{term}%") for term in terms))
        path_contains = or_(*(SitePage.path.ilike(f"%{term}%") for term in terms))
        lexical = case(
            (title_contains, 0.70),
            (path_contains, 0.45),
            else_=0.0,
        )
        return lexical, literal(0.0), or_(title_contains, path_contains)

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
                    select(Document)
                    .where(Document.is_current.is_(True))
                    .options(selectinload(Document.source))
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

        current_document_urls = {item[0] for item in documents}
        with self._factory.begin() as session:
            stale_conditions = [
                SitePage.discovery_source == SiteDiscoverySource.EXISTING_DOCUMENT.value
            ]
            if current_document_urls:
                stale_conditions.append(
                    SitePage.canonical_url.not_in(current_document_urls)
                )
            session.execute(
                update(SitePage)
                .where(*stale_conditions)
                .values(is_active=False, is_indexable=False, updated_at=func.now())
            )

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
        current_document_urls = {url for url, *_ in documents}
        stale_document_filter = SitePage.discovery_source == (
            SiteDiscoverySource.EXISTING_DOCUMENT.value
        )
        stale_document_filter = and_(
            stale_document_filter,
            SitePage.is_active.is_(True),
            (
                SitePage.canonical_url.not_in(current_document_urls)
                if current_document_urls
                else true()
            ),
        )
        stale_documents = session.execute(
            update(SitePage)
            .where(stale_document_filter)
            .values(is_active=False, is_indexable=False, updated_at=func.now())
        )
        bucket.updated += max(0, int(cast(Any, stale_documents).rowcount or 0))
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
        except Exception:  # noqa: BLE001 - one import row must not abort bootstrap
            summary.failed += 1

    @staticmethod
    def _add_import_result(
        summary: SiteMapSyncSummary,
        result: SiteMapWriteResult,
    ) -> None:
        summary.add(result)

    def _upsert_pages_in_session(
        self,
        session: Session,
        pages: Sequence[SitePageUpsert],
        *,
        now: datetime,
    ) -> list[tuple[SitePageUpsert, bool, bool]]:
        unique_pages = tuple({page.canonical_url: page for page in pages}.values())
        if not unique_pages:
            return []
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            values_to_insert = [
                self._page_values(page, now=now) for page in unique_pages
            ]
            statement = postgres_insert(SitePage).values(values_to_insert)
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
                                    SitePage.page_type
                                    == SitePageType.GENERAL_PAGE.value,
                                    _is_specific_page_type(excluded.page_type),
                                ),
                            ),
                            excluded.page_type,
                        ),
                        else_=SitePage.page_type,
                    ),
                    "discovery_source": case(
                        (
                            incoming_source_rank > existing_source_rank,
                            excluded.discovery_source,
                        ),
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
                    "next_crawl_at": case(
                        (SitePage.next_crawl_at.is_(None), excluded.next_crawl_at),
                        else_=SitePage.next_crawl_at,
                    ),
                    "is_active": case(
                        (
                            incoming_source_rank >= existing_source_rank,
                            excluded.is_active,
                        ),
                        else_=SitePage.is_active,
                    ),
                    "is_indexable": case(
                        (
                            incoming_source_rank >= existing_source_rank,
                            excluded.is_indexable,
                        ),
                        (SitePage.is_indexable.is_(False), false()),
                        else_=excluded.is_indexable,
                    ),
                    "updated_at": func.now(),
                },
            ).returning(
                SitePage.canonical_url,
                literal_column("xmax = 0").label("inserted"),
            )
            rows = session.execute(statement).all()
            inserted = {row[0]: bool(row[1]) for row in rows}
            return [
                (
                    page,
                    inserted.get(page.canonical_url, False),
                    not inserted.get(page.canonical_url, False),
                )
                for page in unique_pages
            ]

        outcomes: list[tuple[SitePageUpsert, bool, bool]] = []
        for page in unique_pages:
            existing = session.scalar(
                select(SitePage).where(SitePage.canonical_url == page.canonical_url)
            )
            if existing is None:
                session.add(SitePage(**self._page_values(page, now=now)))
                outcomes.append((page, True, False))
            else:
                self._merge_page(existing, page, now=now)
                outcomes.append((page, False, True))
        session.flush()
        return outcomes

    def _page_values(self, page: SitePageUpsert, *, now: datetime) -> dict[str, Any]:
        parsed = urlsplit(page.canonical_url)
        return {
            "id": uuid.uuid4(),
            "canonical_url": page.canonical_url,
            "host": (parsed.hostname or "").lower(),
            "path": parsed.path or "/",
            "title": page.title,
            "unit": page.unit,
            "content_hash": page.content_hash,
            "page_type": page.page_type.value,
            "discovery_source": page.discovery_source.value,
            "crawl_status": (
                SiteCrawlStatus.DISCOVERED.value
                if page.is_indexable
                else SiteCrawlStatus.EXCLUDED.value
            ),
            "last_discovered_at": now,
            "next_crawl_at": (
                page.next_crawl_at or self._frontier_policy.new_target_due_at(now)
            ),
            "crawl_priority": page.crawl_priority,
            "minimum_depth": page.minimum_depth,
            "is_indexable": page.is_indexable,
            "is_active": page.is_indexable,
        }

    def _merge_page(
        self,
        existing: SitePage,
        page: SitePageUpsert,
        *,
        now: datetime,
    ) -> None:
        if not existing.title or (
            existing.crawl_status
            not in {SiteCrawlStatus.SUCCESS.value, SiteCrawlStatus.UNCHANGED.value}
            and source_priority(page.discovery_source)
            >= source_priority(_source(existing.discovery_source))
        ):
            existing.title = page.title or existing.title
        existing.unit = existing.unit or page.unit
        existing.content_hash = existing.content_hash or page.content_hash
        if existing.page_type == SitePageType.UNKNOWN.value or (
            existing.page_type == SitePageType.GENERAL_PAGE.value
            and page.page_type
            in {
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
        if existing.next_crawl_at is None:
            existing.next_crawl_at = (
                page.next_crawl_at or self._frontier_policy.new_target_due_at(now)
            )
        existing.crawl_priority = max(existing.crawl_priority, page.crawl_priority)
        existing.minimum_depth = min(existing.minimum_depth, page.minimum_depth)
        incoming_is_trusted = source_priority(page.discovery_source) >= source_priority(
            _source(existing.discovery_source)
        )
        if incoming_is_trusted:
            existing.is_active = True
            existing.is_indexable = page.is_indexable
        else:
            existing.is_indexable = existing.is_indexable and page.is_indexable

    @staticmethod
    def _page_ids_in_session(
        session: Session,
        urls: Sequence[str],
    ) -> dict[str, uuid.UUID]:
        unique_urls = tuple(dict.fromkeys(urls))
        if not unique_urls:
            return {}
        rows = session.execute(
            select(SitePage.canonical_url, SitePage.id).where(
                SitePage.canonical_url.in_(unique_urls)
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    def _upsert_links_in_session(
        self,
        session: Session,
        links: Sequence[tuple[uuid.UUID, uuid.UUID, str, SiteLinkType]],
        *,
        now: datetime,
    ) -> tuple[bool, ...]:
        unique: dict[
            tuple[uuid.UUID, uuid.UUID], tuple[uuid.UUID, uuid.UUID, str, SiteLinkType]
        ] = {}
        for link in links:
            key = (link[0], link[1])
            previous = unique.get(key)
            if previous is None or (not previous[2] and link[2]):
                unique[key] = link
        if not unique:
            return ()
        entries = tuple(unique.values())
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = postgres_insert(SiteLink).values(
                [
                    {
                        "id": uuid.uuid4(),
                        "source_page_id": source_id,
                        "target_page_id": target_id,
                        "anchor_text": anchor,
                        "link_type": link_type.value,
                        "first_discovered_at": now,
                        "last_discovered_at": now,
                    }
                    for source_id, target_id, anchor, link_type in entries
                ]
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
            ).returning(literal_column("xmax = 0").label("inserted"))
            return tuple(bool(row[0]) for row in session.execute(statement).all())

        outcomes: list[bool] = []
        for source_id, target_id, anchor, link_type in entries:
            existing = session.scalar(
                select(SiteLink).where(
                    SiteLink.source_page_id == source_id,
                    SiteLink.target_page_id == target_id,
                )
            )
            if existing is None:
                session.add(
                    SiteLink(
                        id=uuid.uuid4(),
                        source_page_id=source_id,
                        target_page_id=target_id,
                        anchor_text=anchor,
                        link_type=link_type.value,
                        first_discovered_at=now,
                        last_discovered_at=now,
                    )
                )
                outcomes.append(True)
            else:
                if anchor:
                    existing.anchor_text = anchor
                if existing.link_type == SiteLinkType.UNKNOWN.value:
                    existing.link_type = link_type.value
                existing.last_discovered_at = now
                outcomes.append(False)
        session.flush()
        return tuple(outcomes)

    @staticmethod
    def _apply_crawl_success(
        page: SitePage,
        *,
        title: str | None,
        content_hash: str,
        http_status: int | None,
        etag: str | None,
        last_modified: str | None,
        now: datetime,
    ) -> None:
        changed = page.content_hash != content_hash
        page.crawl_status = (
            SiteCrawlStatus.SUCCESS.value
            if changed
            else SiteCrawlStatus.UNCHANGED.value
        )
        page.http_status = http_status
        if etag is not None:
            page.etag = etag
        if last_modified is not None:
            page.last_modified = last_modified
        page.last_crawled_at = now
        page.last_successful_crawl_at = now
        page.failure_count = 0
        if changed:
            page.last_changed_at = now
        page.content_hash = content_hash
        if title and title.strip():
            page.title = title.strip()


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
