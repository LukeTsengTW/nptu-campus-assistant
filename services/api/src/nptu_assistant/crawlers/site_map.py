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
from nptu_assistant.crawlers.crawl_policy import DOCUMENT_RESOURCE_SUFFIXES
from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.config import CrawlerSourceConfig, SiteSearchConfig
from nptu_assistant.crawlers.official_units import (
    DocumentSearchScope,
    OfficialUnitDirectory,
)
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchPlan,
)


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


NPTU_CANONICAL_HOMEPAGE_URL = "https://www.nptu.edu.tw/"
NPTU_ROOT_UNIT = "國立屏東大學"
NPTU_ROOT_ALIASES = (NPTU_ROOT_UNIT, "屏東大學", "屏大", "nptu")


class SiteMapQueryTimeout(SearchDeadlineExceeded):
    """Site-map SQL exhausted its bounded sub-budget."""


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
class SiteLinkUpsert:
    target: SitePageUpsert
    anchor_text: str
    link_type: SiteLinkType


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
    lexical_relevance: float
    structural_score: float
    final_score: float
    is_crawlable: bool = True

    @property
    def relevance(self) -> float:
        """Compatibility alias for crawler queue priority."""
        return self.final_score


@dataclass(frozen=True, slots=True)
class SiteMapBatchWriteResult:
    source_created: bool = False
    source_updated: bool = False
    target_created: int = 0
    target_updated: int = 0
    links_created: int = 0
    links_updated: int = 0
    statement_count: int = 0


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

    def add_batch(self, result: SiteMapBatchWriteResult) -> None:
        self.seen += 1 + result.target_created + result.target_updated
        self.created += int(result.source_created) + result.target_created
        self.updated += int(result.source_updated) + result.target_updated
        self.links_created += result.links_created

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
    ) -> SiteMapBatchWriteResult: ...

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
        deadline: SearchDeadline | None = None,
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
    if any(path.endswith(suffix) for suffix in DOCUMENT_RESOURCE_SUFFIXES):
        return SiteLinkType.DOCUMENT
    if anchor_text.strip():
        return SiteLinkType.CONTENT
    return SiteLinkType.UNKNOWN


class SiteMapDiscoveryPolicy:
    """決定網頁地圖是否已具備足夠查詢相關候選。"""

    lexical_threshold = 0.25

    @staticmethod
    def _intent(plan: SearchPlan) -> str:
        return " ".join(
            (plan.query, *plan.retrieval_queries[1:3], *plan.concepts)
        ).casefold()

    @classmethod
    def _scope_correct(
        cls,
        candidate: SiteMapCandidate,
        scope: DocumentSearchScope | None,
    ) -> bool:
        if scope is None:
            return True
        host = candidate.host.casefold().rstrip(".")
        preferred_hosts = {
            value.casefold().rstrip(".") for value in scope.preferred_hosts
        }
        return bool(
            (scope.canonical_unit and candidate.unit == scope.canonical_unit)
            or host in preferred_hosts
            or (scope.homepage_url and candidate.canonical_url == scope.homepage_url)
        )

    @classmethod
    def _is_strong(
        cls,
        plan: SearchPlan,
        candidate: SiteMapCandidate,
        *,
        scope: DocumentSearchScope | None,
    ) -> bool:
        if candidate.lexical_relevance < cls.lexical_threshold:
            return False
        if candidate.failure_count >= 5:
            return False
        if not cls._scope_correct(candidate, scope):
            return False

        intent = cls._intent(plan)
        announcement_intent = any(
            token in intent for token in ("公告", "通知", "訊息", "最新消息")
        )
        document_intent = any(
            token in intent for token in ("文件", "辦法", "表單", "規章", "申請表")
        )
        homepage_intent = any(token in intent for token in ("首頁", "主頁", "home"))
        if candidate.page_type is SitePageType.UNIT_HOMEPAGE:
            if scope is not None:
                return True
            if candidate.canonical_url != NPTU_CANONICAL_HOMEPAGE_URL:
                return False
            return homepage_intent or any(
                alias.casefold() in intent for alias in NPTU_ROOT_ALIASES
            )
        if candidate.page_type is SitePageType.ANNOUNCEMENT_LISTING:
            return announcement_intent or scope is not None
        if candidate.page_type is SitePageType.OFFICIAL_DOCUMENT:
            return document_intent or candidate.lexical_relevance >= 0.45
        return True

    def has_sufficient_candidates(
        self,
        plan: SearchPlan,
        candidates: Collection[SiteMapCandidate],
        *,
        scope: DocumentSearchScope | None,
        minimum: int,
    ) -> bool:
        if minimum <= 0:
            return True
        strong_urls = {
            candidate.canonical_url
            for candidate in candidates
            if candidate.is_crawlable and self._is_strong(plan, candidate, scope=scope)
        }
        return len(strong_urls) >= minimum


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
        deadline: SearchDeadline | None = None,
    ) -> tuple[SiteMapCandidate, ...]:
        return self._repository.find_candidates(
            plan,
            scope=scope,
            allowed_hosts=allowed_hosts,
            limit=limit,
            deadline=deadline,
        )

    @staticmethod
    def has_sufficient_candidates(
        plan: SearchPlan,
        candidates: Collection[SiteMapCandidate],
        *,
        scope: DocumentSearchScope | None,
        minimum: int,
    ) -> bool:
        return SiteMapDiscoveryPolicy().has_sufficient_candidates(
            plan,
            candidates,
            scope=scope,
            minimum=minimum,
        )

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
        source_page = SitePageUpsert(
            canonical_url=page.canonical_url,
            title=page.title.strip() if page.title.strip() else None,
            unit=unit,
            page_type=page_type,
            discovery_source=source,
            crawl_priority=PAGE_TYPE_PRIORITY[page_type],
            minimum_depth=max(0, depth),
        )
        links_by_target: dict[str, SiteLinkUpsert] = {}
        skipped_links = 0
        for link, anchor_text in page.link_texts or tuple(
            (link, "") for link in page.links
        ):
            target = self._normalize_url(link)
            if target is None or not is_allowed_source_url(target, allowed_hosts):
                skipped_links += 1
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
            incoming_link = SiteLinkUpsert(
                target=target_page,
                anchor_text=anchor_text.strip(),
                link_type=(
                    SiteLinkType.ANNOUNCEMENT
                    if is_announcement
                    else classify_link_type(anchor_text, target)
                ),
            )
            previous_link = links_by_target.get(target)
            if previous_link is not None:
                skipped_links += 1
                if not previous_link.anchor_text and incoming_link.anchor_text:
                    links_by_target[target] = incoming_link
                elif (
                    previous_link.link_type is SiteLinkType.UNKNOWN
                    and incoming_link.link_type is not SiteLinkType.UNKNOWN
                ):
                    links_by_target[target] = SiteLinkUpsert(
                        target=previous_link.target,
                        anchor_text=previous_link.anchor_text,
                        link_type=incoming_link.link_type,
                    )
                continue
            links_by_target[target] = incoming_link
        links = tuple(links_by_target.values())
        result = self._repository.persist_fetched_page(
            source_page,
            title=page.title,
            content_hash=content_hash(page.body),
            http_status=http_status,
            etag=etag,
            last_modified=last_modified,
            links=links,
        )
        summary = SiteMapSyncSummary()
        summary.add_batch(result)
        summary.seen += skipped_links
        summary.skipped += skipped_links
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
