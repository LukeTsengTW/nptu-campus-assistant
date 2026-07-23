from __future__ import annotations

from collections.abc import Collection, Mapping

from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuListingItem,
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.official_units import (
    DocumentSearchScope,
    load_default_official_unit_directory,
)
from nptu_assistant.crawlers.site_map import (
    SiteCrawlStatus,
    SiteLinkType,
    SiteMapCandidate,
    SiteMapService,
    SiteMapSyncSummary,
    SiteMapWriteResult,
    SitePageType,
    SitePageUpsert,
    classify_link_type,
    classify_page_type,
)
from nptu_assistant.crawlers.site_models import SearchPlan
from nptu_assistant.crawlers.site_search import NptuSiteSearchService
from nptu_assistant.crawlers.site_models import SearchDeadline


class MemorySiteMapRepository:
    def __init__(self) -> None:
        self.pages: dict[str, SitePageUpsert] = {}
        self.links: list[tuple[str, str, str, SiteLinkType]] = []
        self.successes: list[tuple[str, str]] = []
        self.failures: list[tuple[str, SiteCrawlStatus]] = []
        self.candidates: tuple[SiteMapCandidate, ...] = ()

    def upsert_page(self, page: SitePageUpsert) -> SiteMapWriteResult:
        created = page.canonical_url not in self.pages
        self.pages[page.canonical_url] = page
        return SiteMapWriteResult(created=created, updated=not created)

    def upsert_link(
        self,
        source: SitePageUpsert,
        target: SitePageUpsert,
        *,
        anchor_text: str,
        link_type: SiteLinkType,
    ) -> SiteMapWriteResult:
        self.upsert_page(source)
        self.upsert_page(target)
        edge = (source.canonical_url, target.canonical_url, anchor_text, link_type)
        created = edge not in self.links
        if created:
            self.links.append(edge)
        return SiteMapWriteResult(created=created, updated=not created)

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
        del http_status, etag, last_modified
        self.successes.append((canonical_url, content_hash))
        return SiteMapWriteResult(updated=True)

    def record_crawl_failure(
        self,
        canonical_url: str,
        *,
        http_status: int | None = None,
        status: SiteCrawlStatus = SiteCrawlStatus.FAILED,
    ) -> SiteMapWriteResult:
        del http_status
        self.failures.append((canonical_url, status))
        return SiteMapWriteResult(updated=True)

    def find_candidates(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope | None,
        allowed_hosts: Collection[str],
        limit: int,
    ) -> tuple[SiteMapCandidate, ...]:
        del plan, scope, allowed_hosts
        return self.candidates[:limit]

    def import_existing_urls(self) -> Mapping[str, SiteMapSyncSummary]:
        return {}


class RecordingDiscovery:
    def __init__(self) -> None:
        self.calls = 0

    def discover(self, plan: SearchPlan, *, max_items: int, deadline: SearchDeadline):
        del plan, max_items
        self.calls += 1
        deadline.raise_if_expired()
        raise AssertionError("site map 已有足夠候選時不應執行 live discovery")


class MappingHttpClient:
    def get(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        del allowed_hosts, timeout_seconds
        if deadline is not None:
            deadline.raise_if_expired()
        return f"<html><title>{url}</title><body>研究所推甄報名資訊</body></html>"


class DeterministicScorer:
    def score_candidate(self, plan: SearchPlan, candidate: object) -> float:
        del plan, candidate
        return 1.0

    def score_pages(
        self, plan: SearchPlan, candidates: object, pages: object, **kwargs: object
    ) -> list[float]:
        del plan, candidates, kwargs
        return [1.0 for _page in pages]  # type: ignore[union-attr]


def make_service(repository: MemorySiteMapRepository) -> SiteMapService:
    return SiteMapService(
        repository,
        official_units=load_default_official_unit_directory(),
        source_configs=(),
        site_config=SiteSearchConfig(
            enabled=True,
            seed_urls=["https://www.nptu.edu.tw/"],
            allowed_hosts=["nptu.edu.tw"],
        ),
    )


def test_page_and_link_classification_is_centralized() -> None:
    listing = NptuSitePage(
        title="公告列表",
        canonical_url="https://www.nptu.edu.tw/announcements",
        body="公告",
        published_at=None,
        links=(),
        role=UnitAnnouncementPageRole.LISTING,
    )
    assert classify_page_type(listing) is SitePageType.ANNOUNCEMENT_LISTING
    assert (
        classify_link_type("最新公告", "https://www.nptu.edu.tw/a")
        is SiteLinkType.ANNOUNCEMENT
    )
    assert (
        classify_link_type("下載辦法", "https://www.nptu.edu.tw/a.pdf")
        is SiteLinkType.DOCUMENT
    )


def test_fetched_page_persists_internal_links_and_rejects_external_urls() -> None:
    repository = MemorySiteMapRepository()
    service = make_service(repository)
    detail = NptuListingItem(
        title="招生公告",
        canonical_url="https://www.nptu.edu.tw/announcement/1",
        published_at=None,
        summary="",
        anchor_text="招生公告",
        order=0,
    )
    page = NptuSitePage(
        title="公告列表",
        canonical_url="https://www.nptu.edu.tw/announcements",
        body="公告內容",
        published_at=None,
        links=(
            "https://www.nptu.edu.tw/announcement/1",
            "https://outside.example/ignored",
        ),
        link_texts=(("https://www.nptu.edu.tw/announcement/1", "招生公告"),),
        role=UnitAnnouncementPageRole.LISTING,
        announcement_items=(detail,),
    )

    summary = service.record_fetched_page(
        page,
        unit="國立屏東大學",
        depth=0,
        allowed_hosts=("nptu.edu.tw",),
    )

    assert repository.successes[0][0] == page.canonical_url
    assert len(repository.links) == 1
    source, target, anchor, link_type = repository.links[0]
    assert source == page.canonical_url
    assert target == detail.canonical_url
    assert anchor == "招生公告"
    assert link_type is SiteLinkType.ANNOUNCEMENT
    assert repository.pages[target].page_type is SitePageType.ANNOUNCEMENT_DETAIL
    assert summary.links_created == 1


def test_site_map_candidates_require_quality_not_row_count() -> None:
    repository = MemorySiteMapRepository()
    service = make_service(repository)
    weak = SiteMapCandidate(
        canonical_url="https://www.nptu.edu.tw/weak",
        title=None,
        host="www.nptu.edu.tw",
        unit=None,
        page_type=SitePageType.UNKNOWN,
        crawl_priority=0,
        minimum_depth=0,
        failure_count=0,
        relevance=0.05,
    )
    homepage = weak.__class__(
        canonical_url="https://www.nptu.edu.tw/",
        title="NPTU",
        host="www.nptu.edu.tw",
        unit="國立屏東大學",
        page_type=SitePageType.UNIT_HOMEPAGE,
        crawl_priority=100,
        minimum_depth=0,
        failure_count=0,
        relevance=0.05,
    )
    assert not service.has_sufficient_candidates((weak,), minimum=2)
    strong = homepage.__class__(
        canonical_url="https://www.nptu.edu.tw/announcements",
        title="公告列表",
        host="www.nptu.edu.tw",
        unit="國立屏東大學",
        page_type=SitePageType.ANNOUNCEMENT_LISTING,
        crawl_priority=90,
        minimum_depth=0,
        failure_count=0,
        relevance=0.40,
    )
    assert service.has_sufficient_candidates((weak, homepage), minimum=1)
    assert service.has_sufficient_candidates((weak, homepage, strong), minimum=2)


def test_search_uses_sufficient_site_map_before_live_discovery() -> None:
    repository = MemorySiteMapRepository()
    repository.candidates = (
        SiteMapCandidate(
            canonical_url="https://www.nptu.edu.tw/",
            title="校務資訊",
            host="www.nptu.edu.tw",
            unit=None,
            page_type=SitePageType.GENERAL_PAGE,
            crawl_priority=40,
            minimum_depth=0,
            failure_count=0,
            relevance=0.80,
        ),
    )
    site_map = make_service(repository)
    discovery = RecordingDiscovery()
    service = NptuSiteSearchService(
        SiteSearchConfig(
            enabled=True,
            seed_urls=["https://www.nptu.edu.tw/"],
            allowed_hosts=["nptu.edu.tw"],
            max_pages=1,
            max_items=1,
            max_candidate_urls=5,
            max_depth=0,
            max_pages_per_host=2,
            early_stop_min_results=1,
        ),
        MappingHttpClient(),
        scorer=DeterministicScorer(),  # type: ignore[arg-type]
        discovery=discovery,
        site_map=site_map,
    )

    result = service.search(SearchPlan.from_query("研究所推甄報名", limit=1))

    assert discovery.calls == 0
    assert result.pages
    assert result.pages[0].canonical_url == "https://www.nptu.edu.tw/"
