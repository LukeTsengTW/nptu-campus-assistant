from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuSitePageAdapter,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.config import SiteSearchConfig, load_keyword_search_config
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.official_units import DocumentSearchScope
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDiagnostics,
    SearchPlan,
)
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    SITE_SEARCH_FAILURE_WARNING,
    SITE_SEARCH_PARTIAL_WARNING,
    SiteSearchResult,
    SitePageIngestionService,
)
from nptu_assistant.providers.fake import FakeEmbeddingProvider


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def site_config(**overrides: object) -> SiteSearchConfig:
    values: dict[str, object] = {
        "enabled": True,
        "name": "nptu-domain-search",
        "seed_urls": ["https://www.nptu.edu.tw/"],
        "allowed_hosts": ["nptu.edu.tw"],
        "max_pages": 10,
        "max_items": 5,
        "unit": "國立屏東大學",
        "category": "NPTU 網域搜尋",
    }
    values.update(overrides)
    return SiteSearchConfig.model_validate(values)


def test_project_site_search_is_enabled_and_root_scoped() -> None:
    config = load_keyword_search_config(
        WORKSPACE_ROOT / "data/sources/announcements.yaml"
    )

    assert config.site_search is not None
    assert config.site_search.enabled is True
    assert config.site_search.seed_urls == ["https://www.nptu.edu.tw/"]
    assert config.site_search.allowed_hosts == ["nptu.edu.tw"]


def test_site_search_config_rejects_non_nptu_seed() -> None:
    with pytest.raises(ValueError, match="NPTU"):
        site_config(seed_urls=["https://example.com/"])


def test_site_page_adapter_extracts_date_and_only_crawlable_nptu_links() -> None:
    html = """
    <html><head>
      <title>校外獎學金公告</title>
      <meta property="article:published_time" content="2026-07-10T09:00:00">
    </head><body><main>
      <h1>校外獎學金公告</h1><p>提供獎學金申請資訊。</p>
      <a href="/p/next.php#content">下一頁</a>
      <a href="https://ccs.nptu.edu.tw/p/college.php">校內頁面</a>
      <a href="https://example.com/phishing">外部頁面</a>
      <a href="/files/rules.pdf">PDF</a>
    </main></body></html>
    """

    page = NptuSitePageAdapter().parse_page(
        html,
        "https://www.nptu.edu.tw/",
        allowed_hosts=["nptu.edu.tw"],
    )

    assert page.title == "校外獎學金公告"
    assert page.published_at == date(2026, 7, 10)
    assert page.links == (
        "https://www.nptu.edu.tw/p/next.php",
        "https://ccs.nptu.edu.tw/p/college.php",
    )
    assert "提供獎學金申請資訊" in page.body


def test_site_page_adapter_classifies_listing_and_extracts_structured_items() -> None:
    html = """
    <main><h1>最新公告</h1><div class="module">
      <div class="row listBS"><span class="mtitle"><a href="/p/406-1.php">甲公告</a></span>
        <i class="mdate">2026-07-18</i><span class="summary">甲摘要</span></div>
      <div class="row listBS"><span class="mtitle"><a href="/p/406-2.php">乙公告</a></span>
        <i class="mdate">2026-07-10</i></div>
      <div class="row listBS"><span class="mtitle"><a href="https://example.com/x">外部</a></span></div>
    </div></main>
    """

    page = NptuSitePageAdapter().parse_page(
        html,
        "https://csie.nptu.edu.tw/p/403-1009.php",
        allowed_hosts=["csie.nptu.edu.tw"],
    )

    assert page.role is UnitAnnouncementPageRole.LISTING
    assert [item.title for item in page.announcement_items] == ["甲公告", "乙公告"]
    assert page.announcement_items[0].published_at == date(2026, 7, 18)
    assert page.announcement_items[0].summary == "甲摘要"


def test_site_page_adapter_rejects_navigation_module_as_announcement_listing() -> None:
    page = NptuSitePageAdapter().parse_page(
        """
        <main><h1>學院簡介</h1><div class="module">
          <div class="mtitle"><a href="/p/412-1000-1.php">本院歷史</a></div>
          <div class="mtitle"><a href="/p/412-1000-2.php">組織架構</a></div>
        </div></main>
        """,
        "https://science.nptu.edu.tw/",
        allowed_hosts=["science.nptu.edu.tw"],
    )

    assert page.role is UnitAnnouncementPageRole.OTHER
    assert page.announcement_items == ()


class MappingHttpClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def get(
        self,
        url: str,
        *,
        allowed_hosts: list[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        del timeout_seconds, deadline
        self.calls.append((url, tuple(allowed_hosts or ())))
        return self.pages[url]


def test_site_search_follows_only_allowlisted_links_and_matches_pages() -> None:
    pages = {
        "https://www.nptu.edu.tw/": """
        <main><h1>校首頁</h1><a href="/announcement.php">公告</a>
        <a href="https://example.com/out">外部</a></main>
        """,
        "https://www.nptu.edu.tw/announcement.php": """
        <main><h1>獎學金公告</h1><time datetime="2026-07-10">2026-07-10</time>
        <p>人工智慧獎學金申請資訊。</p></main>
        """,
    }
    http = MappingHttpClient(pages)

    result = NptuSiteSearchService(site_config(), http).search("人工智慧 獎學金")

    assert [page.canonical_url for page in result.pages] == [
        "https://www.nptu.edu.tw/announcement.php",
    ]
    assert result.visited_count == 2
    assert result.failed_count == 0
    assert all(hosts == ("nptu.edu.tw",) for _, hosts in http.calls)


def test_unit_scope_replaces_global_seed_and_rejects_cross_unit_links() -> None:
    pages = {
        "https://csie.nptu.edu.tw/": """
        <main><h1>資訊工程學系</h1>
        <a href="/news.php">最新公告</a>
        <a href="https://mis.nptu.edu.tw/news.php">其他單位公告</a></main>
        """,
        "https://csie.nptu.edu.tw/news.php": """
        <main><h1>資訊工程學系最新公告</h1>
        <time datetime="2026-07-18">2026-07-18</time><p>人工智慧演講。</p></main>
        """,
    }
    http = MappingHttpClient(pages)
    scope = DocumentSearchScope(
        canonical_unit="資訊工程學系",
        homepage_url="https://csie.nptu.edu.tw/",
        preferred_hosts=("csie.nptu.edu.tw",),
        allowed_hosts=("csie.nptu.edu.tw",),
        seed_urls=("https://csie.nptu.edu.tw/",),
    )

    result = NptuSiteSearchService(site_config(), http).search(
        "資訊工程學系 人工智慧 最新公告",
        use_discovery=False,
        scope=scope,
    )

    assert result.pages[0].canonical_url == "https://csie.nptu.edu.tw/news.php"
    assert all(
        page.canonical_url.startswith("https://csie.nptu.edu.tw/")
        for page in result.pages
    )
    assert [url for url, _hosts in http.calls] == [
        "https://csie.nptu.edu.tw/",
        "https://csie.nptu.edu.tw/news.php",
    ]
    assert all(hosts == ("csie.nptu.edu.tw",) for _, hosts in http.calls)


def test_scoped_search_keeps_structurally_verified_listing_below_score_threshold() -> (
    None
):
    homepage = "https://science.nptu.edu.tw/"
    detail_url = "https://science.nptu.edu.tw/p/406-1028-185607.php"
    http = MappingHttpClient(
        {
            homepage: f"""
                <main><h1>理學院</h1><div class="module">
                  <div class="row listBS"><span class="mtitle">
                    <a href="{detail_url}">科學活動公告</a>
                  </span><i class="mdate">2025-07-25</i></div>
                </div></main>
            """
        }
    )
    scope = DocumentSearchScope(
        canonical_unit="理學院",
        homepage_url=homepage,
        preferred_hosts=("science.nptu.edu.tw",),
        allowed_hosts=("science.nptu.edu.tw",),
        seed_urls=(homepage,),
    )

    result = NptuSiteSearchService(
        site_config(
            seed_urls=[homepage],
            allowed_hosts=["science.nptu.edu.tw"],
            max_pages=1,
            relevance_threshold=0.99,
            high_confidence_score=0.99,
        ),
        http,
    ).search(
        "理學院 最新公告",
        max_items=1,
        use_discovery=False,
        scope=scope,
    )

    assert [page.canonical_url for page in result.pages] == [homepage]
    assert result.pages[0].role is UnitAnnouncementPageRole.LISTING


def test_site_search_prioritizes_query_relevant_links_before_unrelated_pages() -> None:
    pages = {
        "https://www.nptu.edu.tw/": """
        <main><h1>校首頁</h1>
        <a href="/unavailable.php">校務連結</a>
        <a href="/special-admission.php" title="大學特殊選才 新生入學資訊">進入</a></main>
        """,
        "https://www.nptu.edu.tw/special-admission.php": """
        <main><h1>大學特殊選才</h1>
        <p>新生入學資訊與招生簡章。</p></main>
        """,
    }
    http = MappingHttpClient(pages)

    result = NptuSiteSearchService(
        site_config(max_pages=2),
        http,
    ).search("特殊選才 新生 入學 資訊")

    assert [page.canonical_url for page in result.pages] == [
        "https://www.nptu.edu.tw/special-admission.php",
    ]
    assert result.visited_count == 2
    assert result.failed_count == 0
    assert [url for url, _hosts in http.calls] == [
        "https://www.nptu.edu.tw/",
        "https://www.nptu.edu.tw/special-admission.php",
    ]


def test_site_page_ingestion_ignores_lower_priority_failed_pages_in_user_warning() -> (
    None
):
    pages = {
        "https://www.nptu.edu.tw/": """
        <main><h1>校首頁</h1>
        <a href="/unavailable.php" title="新生入學資訊">進入</a>
        <a href="/special-admission.php" title="特殊選才 新生入學資訊">進入</a></main>
        """,
        "https://www.nptu.edu.tw/special-admission.php": """
        <main><h1>特殊選才</h1><p>新生入學資訊。</p></main>
        """,
    }
    http = MappingHttpClient(pages)
    config = site_config(max_pages=3)

    result = SitePageIngestionService(
        NptuSiteSearchService(config, http),
        MemoryDocumentRepository(),
        FakeEmbeddingProvider(1536),
        config,
    ).ingest("特殊選才 新生 入學 資訊", max_items=5)

    assert result.summary.created == 1
    assert result.warning is None


class MemoryDocumentRepository:
    def __init__(self) -> None:
        self.hashes: set[tuple[str, str]] = set()
        self.saved: list[tuple[object, str]] = []

    def has_hash(self, canonical_url: str, digest: str) -> bool:
        return (canonical_url, digest) in self.hashes

    def save(self, metadata, raw_text, chunks, embeddings) -> None:
        assert len(chunks) == len(embeddings)
        self.hashes.add((str(metadata.source_url), metadata.version))
        self.saved.append((metadata, raw_text))


class MemoryAnnouncementRepository:
    def __init__(self, *, fail_urls: set[str] | None = None) -> None:
        self.items: dict[str, AnnouncementCandidate] = {}
        self.order: list[str] = []
        self.source_names: list[str] = []
        self.fail_urls = fail_urls or set()

    def merge_source_announcements(
        self,
        candidates: list[AnnouncementCandidate],
        **kwargs: object,
    ) -> list[str]:
        assert len(candidates) == 1
        candidate = candidates[0]
        if candidate.canonical_url in self.fail_urls:
            raise RuntimeError("persistence failed")
        self.items[candidate.canonical_url] = candidate
        self.order.append(candidate.canonical_url)
        self.source_names.append(str(kwargs["source_name"]))
        return ["created"]


class ScopedSearchFixture:
    def __init__(self, listing_page, details: dict[str, object]) -> None:
        self.listing_page = listing_page
        self.details = details

    def search(self, *args: object, **kwargs: object) -> SiteSearchResult:
        del args, kwargs
        return SiteSearchResult(
            (self.listing_page,),
            SearchDiagnostics(relevant_success_count=1),
        )

    def fetch_page(self, url: str, **kwargs: object):
        del kwargs
        return self.details[url]

    def new_deadline(self) -> SearchDeadline:
        return SearchDeadline.after(10)


def test_scoped_announcements_fetch_details_sort_and_persist_only_dated_items() -> None:
    adapter = NptuSitePageAdapter()
    host = "csie.nptu.edu.tw"
    listing_url = f"https://{host}/p/403-1009.php"
    listing = adapter.parse_page(
        """
        <main><h1>最新公告</h1><div class="module">
          <div class="row listBS"><span class="mtitle"><a href="/p/406-new.php">一般最新</a></span><i class="mdate">2026-07-20</i></div>
          <div class="row listBS"><span class="mtitle"><a href="/p/406-ai.php">人工智慧專題</a></span><i class="mdate">2026-07-19</i></div>
          <div class="row listBS"><span class="mtitle"><a href="/p/406-undated.php">無日期公告</a></span></div>
        </div></main>
        """,
        listing_url,
        allowed_hosts=[host],
    )

    def detail(path: str, title: str, published: str | None, body: str):
        meta = (
            f'<meta property="article:published_time" content="{published}">'
            if published
            else ""
        )
        return adapter.parse_page(
            f"<html><head>{meta}</head><body><article><h1>{title}</h1>"
            f"<p>{body}，這是一段足夠長度的正式公告詳情內容，供測試使用。</p></article></body></html>",
            f"https://{host}{path}",
            allowed_hosts=[host],
        )

    details = {
        f"https://{host}/p/406-new.php": detail(
            "/p/406-new.php", "一般最新", "2026-07-20", "一般活動"
        ),
        f"https://{host}/p/406-ai.php": detail(
            "/p/406-ai.php", "人工智慧專題", "2026-07-10", "人工智慧講座"
        ),
        f"https://{host}/p/406-undated.php": detail(
            "/p/406-undated.php", "無日期公告", None, "未標示日期"
        ),
    }
    repository = MemoryAnnouncementRepository()
    config = site_config()
    service = SitePageIngestionService(
        ScopedSearchFixture(listing, details),  # type: ignore[arg-type]
        MemoryDocumentRepository(),
        FakeEmbeddingProvider(1536),
        config,
        repository,
    )
    scope = DocumentSearchScope(
        canonical_unit="資訊工程學系",
        homepage_url=f"https://{host}/",
        preferred_hosts=(host,),
        allowed_hosts=(host,),
        seed_urls=(listing_url,),
    )

    result = service.search_unit_announcements(
        SearchPlan.from_query("資訊工程學系 人工智慧 公告", limit=3),
        scope=scope,
        max_items=3,
        deadline=SearchDeadline.after(10),
        sort="relevance",
        topic="人工智慧",
    )

    assert repository.order[:2] == [
        f"https://{host}/p/406-ai.php",
        f"https://{host}/p/406-new.php",
    ]
    assert repository.items[f"https://{host}/p/406-ai.php"].published_at == date(
        2026, 7, 10
    )
    assert result.persisted_count == 2
    assert result.undated_count == 1
    assert result.warning == SITE_SEARCH_PARTIAL_WARNING
    assert set(result.canonical_urls) == set(repository.items)
    assert repository.source_names == [
        "unit-scoped:資訊工程學系",
        "unit-scoped:資訊工程學系",
    ]

    service.search_unit_announcements(
        SearchPlan.from_query("資訊工程學系 最新公告", limit=3),
        scope=scope,
        max_items=3,
        deadline=SearchDeadline.after(10),
        sort="newest",
    )
    assert len(repository.items) == 2
    assert repository.order[-2:] == [
        f"https://{host}/p/406-new.php",
        f"https://{host}/p/406-ai.php",
    ]

    new_url = f"https://{host}/p/406-new.php"
    ai_url = f"https://{host}/p/406-ai.php"
    partial_repository = MemoryAnnouncementRepository(fail_urls={new_url})
    partial_service = SitePageIngestionService(
        ScopedSearchFixture(listing, details),  # type: ignore[arg-type]
        MemoryDocumentRepository(),
        FakeEmbeddingProvider(1536),
        config,
        partial_repository,
    )
    partial = partial_service.search_unit_announcements(
        SearchPlan.from_query("資訊工程學系 最新公告", limit=3),
        scope=scope,
        max_items=3,
        deadline=SearchDeadline.after(10),
        sort="newest",
    )
    assert partial.canonical_urls == (ai_url,)
    assert partial.failed_count == 1
    assert partial.warning == SITE_SEARCH_PARTIAL_WARNING

    failed_repository = MemoryAnnouncementRepository(fail_urls={new_url, ai_url})
    failed_service = SitePageIngestionService(
        ScopedSearchFixture(listing, details),  # type: ignore[arg-type]
        MemoryDocumentRepository(),
        FakeEmbeddingProvider(1536),
        config,
        failed_repository,
    )
    failed = failed_service.search_unit_announcements(
        SearchPlan.from_query("資訊工程學系 最新公告", limit=3),
        scope=scope,
        max_items=3,
        deadline=SearchDeadline.after(10),
        sort="newest",
    )
    assert failed.canonical_urls == ()
    assert failed.failed_count == 2
    assert failed.warning == SITE_SEARCH_FAILURE_WARNING


def test_site_page_ingestion_indexes_pages_without_pretending_they_are_announcements() -> (
    None
):
    html = "<main><h1>校務資訊</h1><p>人工智慧課程與申請說明。</p></main>"
    http = MappingHttpClient({"https://www.nptu.edu.tw/": html})
    config = site_config()
    search = NptuSiteSearchService(config, http)
    repository = MemoryDocumentRepository()

    result = SitePageIngestionService(
        search,
        repository,
        FakeEmbeddingProvider(1536),
        config,
    ).ingest("人工智慧", max_items=5)

    assert result.summary.created == 1
    assert result.summary.failed == 0
    assert result.relevant_pages_found == 1
    assert result.relevant_pages_persisted == 1
    assert result.ingestion_timed_out is False
    assert result.ingestion_complete is True
    assert len(repository.saved) == 1
    metadata, raw_text = repository.saved[0]
    assert metadata.document_type == "official_web_page"
    assert metadata.published_at is None
    assert metadata.effective_from == date.today()
    assert "人工智慧課程" in raw_text
