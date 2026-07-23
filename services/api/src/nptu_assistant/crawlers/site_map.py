from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from nptu_assistant.core.security import (
    canonicalize_nptu_url,
    is_allowed_nptu_url,
    is_allowed_source_url,
)
from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.config import CrawlerSourceConfig, SiteSearchConfig
from nptu_assistant.crawlers.official_units import (
    DocumentSearchScope,
    OfficialUnitDirectory,
)
from nptu_assistant.crawlers.site_models import SearchPlan


class SitePageType(StrEnum):
    UNIT_HOMEPAGE = "unit_homepage"
    ANNOUNCEMENT_LISTING = "announcement_listing"
    ANNOUNCEMENT_DETAIL = "announcement_detail"
    OFFICIAL_DOCUMENT = "official_document"
    GENERAL_PAGE = "general_page"
    SEARCH_RESULT = "search_result"
    UNKNOWN = "unknown"


class SiteDiscoverySource(StrEnum):
    OFFICIAL_UNIT = "official_unit"
    CONFIGURED_SEED = "configured_seed"
    OFFICIAL_SEARCH = "official_search"
    INTERNAL_LINK = "internal_link"
    EXISTING_SOURCE = "existing_source"
    EXISTING_DOCUMENT = "existing_document"
    EXISTING_ANNOUNCEMENT = "existing_announcement"
    MANUAL = "manual"


class SiteCrawlStatus(StrEnum):
    DISCOVERED = "discovered"
    QUEUED = "queued"
    FETCHING = "fetching"
    SUCCESS = "success"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    BLOCKED = "blocked"
    EXCLUDED = "excluded"


class SiteLinkType(StrEnum):
    NAVIGATION = "navigation"
    CONTENT = "content"
    ANNOUNCEMENT = "announcement"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


PAGE_TYPE_PRIORITY: Mapping[SitePageType, int] = {
    SitePageType.UNIT_HOMEPAGE: 100,
    SitePageType.ANNOUNCEMENT_LISTING: 90,
    SitePageType.ANNOUNCEMENT_DETAIL: 80,
    SitePageType.OFFICIAL_DOCUMENT: 70,
    SitePageType.GENERAL_PAGE: 40,
    SitePageType.SEARCH_RESULT: 20,
    SitePageType.UNKNOWN: 0,
}

DISCOVERY_SOURCE_PRIORITY: Mapping[SiteDiscoverySource, int] = {
    SiteDiscoverySource.OFFICIAL_UNIT: 100,
    SiteDiscoverySource.EXISTING_DOCUMENT: 95,
    SiteDiscoverySource.EXISTING_ANNOUNCEMENT: 95,
    SiteDiscoverySource.EXISTING_SOURCE: 85,
    SiteDiscoverySource.CONFIGURED_SEED: 80,
    SiteDiscoverySource.MANUAL: 60,
    SiteDiscoverySource.OFFICIAL_SEARCH: 50,
    SiteDiscoverySource.INTERNAL_LINK: 30,
}


@dataclass(frozen=True, slots=True)
class SitePageUpsert:
    canonical_url: str
    title: str | None = None
    unit: str | None = None
    content_hash: str | None = None
    page_type: SitePageType = SitePageType.UNKNOWN
    discovery_source: SiteDiscoverySource = SiteDiscoverySource.MANUAL
    crawl_priority: int = 0
    minimum_depth: int = 0
    is_indexable: bool = True


@dataclass(frozen=True, slots=True)
class SiteMapCandidate:
    canonical_url: str
    title: str | None
    host: str
    unit: str | None
    page_type: SitePageType
    crawl_priority: int
    minimum_depth: int
    failure_count: int
    relevance: float


@dataclass(frozen=True, slots=True)
class SiteMapWriteResult:
    created: bool = False
    updated: bool = False


@dataclass
class SiteMapSyncSummary:
    seen: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    links_created: int = 0
    breakdown: dict[str, "SiteMapSyncSummary"] = field(default_factory=dict, repr=False)

    def add(self, result: SiteMapWriteResult) -> None:
        if result.created:
            self.created += 1
        elif result.updated:
            self.updated += 1
        else:
            self.skipped += 1

    def merge(self, other: "SiteMapSyncSummary") -> None:
        self.seen += other.seen
        self.created += other.created
        self.updated += other.updated
        self.skipped += other.skipped
        self.failed += other.failed
        self.links_created += other.links_created


class SiteMapRepository(Protocol):
    def upsert_page(self, page: SitePageUpsert) -> SiteMapWriteResult: ...

    def upsert_link(
        self,
        source: SitePageUpsert,
        target: SitePageUpsert,
        *,
        anchor_text: str,
        link_type: SiteLinkType,
    ) -> SiteMapWriteResult: ...

    def record_crawl_success(
        self,
        canonical_url: str,
        *,
        title: str | None,
        content_hash: str,
        http_status: int | None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SiteMapWriteResult: ...

    def record_crawl_failure(
        self,
        canonical_url: str,
        *,
        http_status: int | None = None,
        status: SiteCrawlStatus = SiteCrawlStatus.FAILED,
    ) -> SiteMapWriteResult: ...

    def find_candidates(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope | None,
        allowed_hosts: Collection[str],
        limit: int,
    ) -> tuple[SiteMapCandidate, ...]: ...

    def import_existing_urls(self) -> Mapping[str, SiteMapSyncSummary]: ...


def classify_page_type(page: NptuSitePage) -> SitePageType:
    if page.role is UnitAnnouncementPageRole.LISTING or page.announcement_items:
        return SitePageType.ANNOUNCEMENT_LISTING
    if page.role is UnitAnnouncementPageRole.DETAIL:
        return SitePageType.ANNOUNCEMENT_DETAIL
    return SitePageType.GENERAL_PAGE


def classify_link_type(anchor_text: str, target_url: str) -> SiteLinkType:
    normalized_anchor = anchor_text.casefold()
    path = urlsplit(target_url).path.casefold()
    if any(token in normalized_anchor for token in ("公告", "通知", "訊息")):
        return SiteLinkType.ANNOUNCEMENT
    if path.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".odt")):
        return SiteLinkType.DOCUMENT
    if anchor_text.strip():
        return SiteLinkType.CONTENT
    return SiteLinkType.UNKNOWN


class SiteMapService:
    def __init__(
        self,
        repository: SiteMapRepository,
        *,
        official_units: OfficialUnitDirectory,
        source_configs: Sequence[CrawlerSourceConfig],
        site_config: SiteSearchConfig,
    ) -> None:
        self._repository = repository
        self._official_units = official_units
        self._source_configs = tuple(source_configs)
        self._site_config = site_config

    def find_candidates(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope | None,
        allowed_hosts: Collection[str],
        limit: int,
    ) -> tuple[SiteMapCandidate, ...]:
        return self._repository.find_candidates(
            plan,
            scope=scope,
            allowed_hosts=allowed_hosts,
            limit=limit,
        )

    @staticmethod
    def has_sufficient_candidates(
        candidates: Collection[SiteMapCandidate],
        *,
        minimum: int,
    ) -> bool:
        strong = [
            item
            for item in candidates
            if item.relevance >= 0.30
            or item.page_type
            in {
                SitePageType.UNIT_HOMEPAGE,
                SitePageType.ANNOUNCEMENT_LISTING,
            }
        ]
        return len(strong) >= minimum

    def record_discovery(
        self,
        canonical_url: str,
        *,
        title: str | None = None,
        unit: str | None = None,
        source: SiteDiscoverySource = SiteDiscoverySource.OFFICIAL_SEARCH,
        page_type: SitePageType = SitePageType.SEARCH_RESULT,
        depth: int = 0,
        allowed_hosts: Collection[str] | None = None,
    ) -> SiteMapWriteResult:
        normalized = self._normalize_url(canonical_url, allowed_hosts=allowed_hosts)
        if normalized is None:
            return SiteMapWriteResult()
        return self._repository.upsert_page(
            SitePageUpsert(
                canonical_url=normalized,
                title=title.strip() if title and title.strip() else None,
                unit=unit,
                page_type=page_type,
                discovery_source=source,
                crawl_priority=PAGE_TYPE_PRIORITY[page_type],
                minimum_depth=max(0, depth),
            )
        )

    def record_fetched_page(
        self,
        page: NptuSitePage,
        *,
        unit: str | None,
        depth: int,
        allowed_hosts: Collection[str],
        http_status: int | None = 200,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> SiteMapSyncSummary:
        from nptu_assistant.ingestion.cleaning import content_hash

        page_type = classify_page_type(page)
        source = SiteDiscoverySource.INTERNAL_LINK
        page_result = self.record_discovery(
            page.canonical_url,
            title=page.title,
            unit=unit,
            source=source,
            page_type=page_type,
            depth=depth,
            allowed_hosts=allowed_hosts,
        )
        crawl_result = self._repository.record_crawl_success(
            page.canonical_url,
            title=page.title,
            content_hash=content_hash(page.body),
            http_status=http_status,
            etag=etag,
            last_modified=last_modified,
        )
        summary = SiteMapSyncSummary(seen=1)
        summary.add(page_result)
        if crawl_result.updated:
            summary.updated += 1
        elif crawl_result.created:
            summary.created += 1
        for link, anchor_text in page.link_texts or tuple(
            (link, "") for link in page.links
        ):
            target = self._normalize_url(link)
            if target is None or not is_allowed_source_url(target, allowed_hosts):
                summary.skipped += 1
                continue
            is_announcement = target in {
                item.canonical_url for item in page.announcement_items
            }
            target_type = (
                SitePageType.ANNOUNCEMENT_DETAIL
                if is_announcement
                else SitePageType.UNKNOWN
            )
            target_source = SiteDiscoverySource.INTERNAL_LINK
            target_page = SitePageUpsert(
                canonical_url=target,
                title=anchor_text.strip() or None,
                unit=unit,
                page_type=target_type,
                discovery_source=target_source,
                crawl_priority=PAGE_TYPE_PRIORITY[target_type],
                minimum_depth=max(0, depth + 1),
                is_indexable=not target.lower().endswith(
                    (".pdf", ".doc", ".docx", ".xls", ".xlsx")
                ),
            )
            try:
                link_result = self._repository.upsert_link(
                    SitePageUpsert(
                        canonical_url=page.canonical_url,
                        unit=unit,
                        page_type=page_type,
                        discovery_source=source,
                        crawl_priority=PAGE_TYPE_PRIORITY[page_type],
                        minimum_depth=max(0, depth),
                    ),
                    target_page,
                    anchor_text=anchor_text,
                    link_type=(
                        SiteLinkType.ANNOUNCEMENT
                        if is_announcement
                        else classify_link_type(anchor_text, target)
                    ),
                )
            except Exception:
                summary.failed += 1
                continue
            summary.add(link_result)
            if link_result.created:
                summary.links_created += 1
        return summary

    def record_crawl_failure(
        self,
        canonical_url: str,
        *,
        http_status: int | None = None,
        status: SiteCrawlStatus = SiteCrawlStatus.FAILED,
    ) -> SiteMapWriteResult:
        normalized = self._normalize_url(canonical_url)
        if normalized is None:
            return SiteMapWriteResult()
        return self._repository.record_crawl_failure(
            normalized,
            http_status=http_status,
            status=status,
        )

    def sync(self) -> SiteMapSyncSummary:
        total = SiteMapSyncSummary()
        categories: dict[str, SiteMapSyncSummary] = {}

        def add_discovery(
            category: str,
            url: str,
            **kwargs: object,
        ) -> None:
            bucket = categories.setdefault(category, SiteMapSyncSummary())
            bucket.seen += 1
            total.seen += 1
            try:
                result = self.record_discovery(url, **kwargs)  # type: ignore[arg-type]
            except Exception:
                bucket.failed += 1
                total.failed += 1
                return
            bucket.add(result)
            total.add(result)

        for unit in self._official_units.active_units:
            if unit.homepage_url:
                add_discovery(
                    "official units",
                    unit.homepage_url,
                    unit=unit.canonical_name,
                    source=SiteDiscoverySource.OFFICIAL_UNIT,
                    page_type=SitePageType.UNIT_HOMEPAGE,
                    allowed_hosts=unit.allowed_hosts,
                )
            for seed_url in unit.seed_urls:
                add_discovery(
                    "official units",
                    seed_url,
                    unit=unit.canonical_name,
                    source=SiteDiscoverySource.CONFIGURED_SEED,
                    page_type=SitePageType.GENERAL_PAGE,
                    allowed_hosts=unit.allowed_hosts,
                )

        for seed_url in self._site_config.seed_urls:
            add_discovery(
                "configured seeds",
                seed_url,
                source=SiteDiscoverySource.CONFIGURED_SEED,
                page_type=SitePageType.GENERAL_PAGE,
                allowed_hosts=self._site_config.allowed_hosts,
            )

        for source_config in self._source_configs:
            add_discovery(
                "configured source URLs",
                source_config.url,
                unit=source_config.unit,
                source=SiteDiscoverySource.CONFIGURED_SEED,
                page_type=SitePageType.ANNOUNCEMENT_LISTING,
                allowed_hosts=source_config.allowed_hosts,
            )

        try:
            existing = self._repository.import_existing_urls()
        except Exception:
            total.failed += 1
        else:
            for category, bucket in existing.items():
                categories.setdefault(category, SiteMapSyncSummary()).merge(bucket)
                total.merge(bucket)

        total.breakdown = categories
        return total

    def _normalize_url(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
    ) -> str | None:
        try:
            normalized = canonicalize_nptu_url(url)
        except ValueError:
            return None
        if not is_allowed_nptu_url(normalized):
            return None
        if allowed_hosts is not None and not is_allowed_source_url(
            normalized, allowed_hosts
        ):
            return None
        return normalized


def source_priority(source: SiteDiscoverySource) -> int:
    return DISCOVERY_SOURCE_PRIORITY[source]


def page_priority(page_type: SitePageType) -> int:
    return PAGE_TYPE_PRIORITY[page_type]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
