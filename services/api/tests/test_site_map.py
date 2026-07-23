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
    SiteMapBatchWriteResult,
    SiteLinkUpsert,
    SiteMapService,
    SiteMapSyncSummary,
    SiteMapWriteResult,
    SitePageType,
    SitePageUpsert,
    classify_link_type,
    classify_page_type,
)
from nptu_assistant.crawlers.site_models import (
    DiscoveredPage,
    SearchDeadlineExceeded,
    SearchPlan,
)
from nptu_assistant.crawlers.site_search import NptuSiteSearchService
from nptu_assistant.crawlers.site_models import SearchDeadline


class MemorySiteMapRepository:
    def __init__(self) -> None:
        self.pages: dict[str, SitePageUpsert] = {}
        self.links: list[tuple[str, str, str, SiteLinkType]] = []
        self.successes: list[tuple[str, str]] = []
        self.failures: list[tuple[str, SiteCrawlStatus]] = []
        self.candidates: tuple[SiteMapCandidate, ...] = ()
        self.batch_calls = 0

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

    def persist_fetched_page(
        self,
        source: SitePageUpsert,
        *,
        title: str | None,
        content_hash: str,
        http_status: int | None,
        etag: str | None = None,
        last_modified: str | None = None,
        links: tuple[SiteLinkUpsert, ...] = (),
    ) -> SiteMapBatchWriteResult:
        del title, http_status, etag, last_modified
        self.batch_calls += 1
        self.upsert_page(source)
        self.successes.append((source.canonical_url, content_hash))
        created_targets = 0
        updated_targets = 0
        created_links = 0
        updated_links = 0
        for link in links:
            target_created = link.target.canonical_url not in self.pages
            result = self.upsert_link(
                source,
                link.target,
                anchor_text=link.anchor_text,
                link_type=link.link_type,
            )
            if result.created:
                created_links += 1
            elif result.updated:
                updated_links += 1
            if target_created:
                created_targets += 1
            else:
                updated_targets += 1
        return SiteMapBatchWriteResult(
            source_created=False,
            source_updated=True,
            target_created=created_targets,
            target_updated=updated_targets,
            links_created=created_links,
            links_updated=updated_links,
        )

    def find_candidates(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope | None,
        allowed_hosts: Collection[str],
        limit: int,
        deadline: SearchDeadline | None = None,
    ) -> tuple[SiteMapCandidate, ...]:
        del plan, scope, allowed_hosts, deadline
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


class SuccessfulDiscovery:
    def __init__(self) -> None:
        self.calls = 0

    def discover(self, plan: SearchPlan, *, max_items: int, deadline: SearchDeadline):
        del plan, max_items
        self.calls += 1
        deadline.raise_if_expired()
        return (DiscoveredPage("https://www.nptu.edu.tw/discovered", "相關頁面", 1.0),)


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
    assert repository.batch_calls == 1


def candidate(
    url: str,
    *,
    title: str | None = None,
    unit: str | None = None,
    page_type: SitePageType = SitePageType.GENERAL_PAGE,
    lexical: float = 0.0,
    structural: float = 0.0,
) -> SiteMapCandidate:
    return SiteMapCandidate(
        canonical_url=url,
        title=title,
        host="www.nptu.edu.tw",
        unit=unit,
        page_type=page_type,
        crawl_priority=40,
        minimum_depth=0,
        failure_count=0,
        lexical_relevance=lexical,
        structural_score=structural,
        final_score=lexical + structural,
    )


def test_unrelated_global_homepages_do_not_skip_discovery() -> None:
    service = make_service(MemorySiteMapRepository())
    candidates = tuple(
        candidate(
            f"https://unit-{index}.nptu.edu.tw/",
            title=f"第 {index} 學系首頁",
            page_type=SitePageType.UNIT_HOMEPAGE,
            lexical=0.01,
            structural=0.9,
        )
        for index in range(10)
    )
    plan = SearchPlan.from_query("低收入戶獎學金申請期限", limit=4)
    assert not service.has_sufficient_candidates(
        plan, candidates, scope=None, minimum=4
    )


def test_unrelated_announcement_listings_do_not_skip_discovery() -> None:
    service = make_service(MemorySiteMapRepository())
    candidates = tuple(
        candidate(
            f"https://unit-{index}.nptu.edu.tw/announcements",
            title=f"第 {index} 學系公告",
            page_type=SitePageType.ANNOUNCEMENT_LISTING,
            lexical=0.02,
            structural=0.9,
        )
        for index in range(5)
    )
    plan = SearchPlan.from_query("宿舍冷氣費計算", limit=4)
    assert not service.has_sufficient_candidates(
        plan, candidates, scope=None, minimum=4
    )


def test_scoped_relevant_unit_pages_can_skip_discovery() -> None:
    service = make_service(MemorySiteMapRepository())
    scope = DocumentSearchScope(
        canonical_unit="資訊工程學系",
        homepage_url="https://csie.nptu.edu.tw/",
        preferred_hosts=("csie.nptu.edu.tw",),
        allowed_hosts=("csie.nptu.edu.tw",),
        seed_urls=("https://csie.nptu.edu.tw/",),
    )
    candidates = (
        candidate(
            "https://csie.nptu.edu.tw/",
            title="資訊工程學系首頁",
            unit="資訊工程學系",
            page_type=SitePageType.UNIT_HOMEPAGE,
            lexical=0.72,
            structural=0.8,
        ),
        candidate(
            "https://csie.nptu.edu.tw/news",
            title="資訊工程學系公告",
            unit="資訊工程學系",
            page_type=SitePageType.ANNOUNCEMENT_LISTING,
            lexical=0.75,
            structural=0.8,
        ),
    )
    plan = SearchPlan.from_query("資訊工程學系最新公告", limit=2)
    assert service.has_sufficient_candidates(plan, candidates, scope=scope, minimum=2)


def test_global_homepage_intent_can_skip_for_exact_official_homepage() -> None:
    service = make_service(MemorySiteMapRepository())
    homepage = candidate(
        "https://www.nptu.edu.tw/",
        title="國立屏東大學首頁",
        unit="國立屏東大學",
        page_type=SitePageType.UNIT_HOMEPAGE,
        lexical=0.66,
        structural=0.9,
    )
    plan = SearchPlan.from_query("屏東大學首頁", limit=1)
    assert service.has_sufficient_candidates(plan, (homepage,), scope=None, minimum=1)


def test_anchor_and_concept_relevance_is_distinct_from_structure() -> None:
    relevant = candidate(
        "https://www.nptu.edu.tw/aid",
        title="弱勢學生助學計畫",
        lexical=0.78,
        structural=0.1,
    )
    unrelated = candidate(
        "https://www.nptu.edu.tw/",
        title="行政單位首頁",
        page_type=SitePageType.UNIT_HOMEPAGE,
        lexical=0.02,
        structural=1.0,
    )
    assert relevant.lexical_relevance > unrelated.lexical_relevance
    assert relevant.final_score < unrelated.final_score


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
            lexical_relevance=0.80,
            structural_score=0.20,
            final_score=0.90,
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


def test_site_map_timeout_fails_open_to_official_discovery() -> None:
    class TimeoutRepository(MemorySiteMapRepository):
        def find_candidates(
            self,
            plan: SearchPlan,
            *,
            scope: DocumentSearchScope | None,
            allowed_hosts: Collection[str],
            limit: int,
            deadline: SearchDeadline | None = None,
        ) -> tuple[SiteMapCandidate, ...]:
            del plan, scope, allowed_hosts, limit, deadline
            raise SearchDeadlineExceeded("test site map timeout")

    discovery = SuccessfulDiscovery()
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
        site_map=SiteMapService(
            TimeoutRepository(),
            official_units=load_default_official_unit_directory(),
            source_configs=(),
            site_config=SiteSearchConfig(
                enabled=True,
                seed_urls=["https://www.nptu.edu.tw/"],
                allowed_hosts=["nptu.edu.tw"],
            ),
        ),
    )
    result = service.search(SearchPlan.from_query("宿舍冷氣費", limit=1))
    assert discovery.calls == 1
    assert result.diagnostics.query_timed_out is False
