from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from nptu_assistant.crawlers.adapters.nptu_search import NptuAssociationSearchAdapter
from nptu_assistant.crawlers.config import KeywordSearchConfig, load_keyword_search_config
from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.search import (
    FULL_SEARCH_FAILURE_WARNING,
    PARTIAL_SEARCH_FAILURE_WARNING,
    KeywordAliasResolver,
    KeywordAnnouncementSearchService,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP_FIXTURE = WORKSPACE_ROOT / "data/fixtures/announcements/search-bootstrap.html"
FORM_FIXTURE = WORKSPACE_ROOT / "data/fixtures/announcements/search-form.html"
RESULT_FIXTURE = WORKSPACE_ROOT / "data/fixtures/announcements/search-results.html"


def keyword_config(**overrides: object) -> KeywordSearchConfig:
    values: dict[str, object] = {
        "name": "nptu-association-search",
        "session_url": "https://www.nptu.edu.tw/app/index.php?Plugin=asso",
        "bootstrap_url": "https://www.nptu.edu.tw/app/index.php?Action=mobileloadmod&Type=mobilesch&Nbr=0",
        "bootstrap_method": "post",
        "url": "https://www.nptu.edu.tw/app/index.php?Plugin=asso&Action=assosearch",
        "search_types": ["part", "com"],
        "max_items": 20,
        "unit": "國立屏東大學",
        "category": "關鍵字搜尋",
        "aliases": {"電科系": "電腦科學與人工智慧學系"},
    }
    values.update(overrides)
    return KeywordSearchConfig.model_validate(values)


def test_keyword_search_config_and_alias_expansion() -> None:
    config = load_keyword_search_config(WORKSPACE_ROOT / "data/sources/announcements.yaml")
    expansion = KeywordAliasResolver(config.aliases).expand("電科系 獎學金")

    assert config.search_types == ["part", "com"]
    assert config.session_url == "https://www.nptu.edu.tw/app/index.php?Plugin=asso"
    assert config.bootstrap_url.endswith("Action=mobileloadmod&Type=mobilesch&Nbr=0")
    assert config.bootstrap_method == "post"
    assert config.max_items == 20
    assert expansion.search_terms == (
        "電科系 獎學金",
        "電腦科學與人工智慧學系 獎學金",
    )
    assert expansion.retrieval_query == "電腦科學與人工智慧學系 獎學金"
    assert KeywordAliasResolver(config.aliases).normalize("電科系") == "電腦科學與人工智慧學系"


def test_search_adapter_parses_form_and_results() -> None:
    adapter = NptuAssociationSearchAdapter()
    bootstrap = adapter.parse_bootstrap_form(
        BOOTSTRAP_FIXTURE.read_text(encoding="utf-8"),
        "https://www.nptu.edu.tw/app/index.php?Plugin=asso",
    )
    form = adapter.parse_form(
        FORM_FIXTURE.read_text(encoding="utf-8"),
        "https://www.nptu.edu.tw/app/index.php?Plugin=asso&Action=assosearch",
    )
    results = adapter.parse_results(RESULT_FIXTURE.read_text(encoding="utf-8"), form.action_url)

    assert bootstrap.method == "post"
    assert bootstrap.hidden_fields == {
        "verify_code": "bootstrap-code",
        "verify_hdcode": "bootstrap-hdcode",
    }
    assert form.method == "post"
    assert form.action_url == "https://www.nptu.edu.tw/app/index.php?Action=assosearch"
    assert form.hidden_fields == {"verify_code": "fixture-code"}
    assert form.search_types == ("part", "com")
    assert results[0].title == "人工智慧學程公告"
    assert results[0].canonical_url.startswith("https://csai.nptu.edu.tw/")
    assert results[0].unit == "電腦科學與人工智慧學系"
    assert results[0].published_at == date(2026, 7, 12)
    assert results[1].published_at is None
    assert len(results) == 2


def test_search_adapter_rejects_unsafe_or_changed_pages() -> None:
    adapter = NptuAssociationSearchAdapter()
    unsafe = FORM_FIXTURE.read_text(encoding="utf-8").replace(
        "/app/index.php?Action=assosearch", "https://example.com/search"
    )

    with pytest.raises(ValueError, match="allowlist"):
        adapter.parse_form(unsafe, "https://www.nptu.edu.tw/app/index.php")
    with pytest.raises(ValueError, match="搜尋結果區塊"):
        adapter.parse_results("<html><body><nav>導覽</nav></body></html>", "https://www.nptu.edu.tw/")


def test_http_client_submits_get_and_post_forms_with_session_cookie() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if request.url.path == "/landing":
            return httpx.Response(200, headers={"Set-Cookie": "session=abc; Path=/"}, text="landing", request=request)
        assert request.headers["cookie"] == "session=abc"
        return httpx.Response(200, text="results", request=request)

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        client.get("https://www.nptu.edu.tw/landing")
        assert client.submit_form("get", "https://www.nptu.edu.tw/search", {"SchKey": "電科系"}) == "results"
        assert client.submit_form("post", "https://www.nptu.edu.tw/search", {"SchType": "part"}) == "results"
    finally:
        client.close()

    search_requests = [request for request in requests if request.url.path == "/search"]
    assert search_requests[0].url.params["SchKey"] == "電科系"
    assert search_requests[1].method == "POST"
    assert search_requests[1].content == b"SchType=part"


class MemoryAnnouncementRepository:
    def __init__(self) -> None:
        self.urls: set[str] = set()
        self.candidates: list[AnnouncementCandidate] = []

    def upsert(
        self,
        candidate: AnnouncementCandidate,
        *,
        source_name: str,
        source_url: str,
        interval_minutes: int,
    ) -> str:
        assert source_name == "nptu-association-search"
        assert source_url.startswith("https://www.nptu.edu.tw/")
        assert interval_minutes == 60
        self.candidates.append(candidate)
        if candidate.canonical_url in self.urls:
            return "unchanged"
        self.urls.add(candidate.canonical_url)
        return "created"


class SearchHttpClient:
    def __init__(self, *, failed_types: set[str] | None = None) -> None:
        self.failed_types = failed_types or set()
        self.submissions: list[tuple[str, str]] = []
        self.form_requests: list[tuple[str, str, dict[str, str]]] = []
        self.gets: list[str] = []

    def get(self, url: str) -> str:
        self.gets.append(url)
        if "Plugin=asso" in url:
            return "<html><body>session established</body></html>"
        return "<main><h1>人工智慧學程公告</h1><p>完整公告內容</p></main>"

    def submit_form(self, method: str, url: str, fields: dict[str, str]) -> str:
        self.form_requests.append((method, url, dict(fields)))
        if "Action=mobileloadmod" in url:
            return BOOTSTRAP_FIXTURE.read_text(encoding="utf-8")
        self.submissions.append((fields["SchKey"], fields["SchType"]))
        if fields["SchType"] in self.failed_types:
            raise RuntimeError("search unavailable")
        return RESULT_FIXTURE.read_text(encoding="utf-8")


def test_keyword_search_service_submits_variants_deduplicates_and_ingests() -> None:
    repository = MemoryAnnouncementRepository()
    http = SearchHttpClient()
    service = KeywordAnnouncementSearchService(keyword_config(), repository, http)

    result = service.ingest("電科系")

    assert http.submissions == [
        ("電科系", "part"),
        ("電科系", "com"),
        ("電腦科學與人工智慧學系", "part"),
        ("電腦科學與人工智慧學系", "com"),
    ]
    assert http.gets[0] == "https://www.nptu.edu.tw/app/index.php?Plugin=asso"
    assert http.form_requests[0] == (
        "post",
        "https://www.nptu.edu.tw/app/index.php?Action=mobileloadmod&Type=mobilesch&Nbr=0",
        {},
    )
    assert result.retrieval_query == "電腦科學與人工智慧學系"
    assert result.summary.created == 1
    assert result.summary.failed == 1
    assert result.warning == PARTIAL_SEARCH_FAILURE_WARNING
    assert result.canonical_urls == (
        "https://csai.nptu.edu.tw/p/406-1096-197001.php?Lang=zh-tw",
    )
    assert len(repository.candidates) == 1
    assert repository.candidates[0].body == "人工智慧學程公告\n完整公告內容"


def test_keyword_search_service_reports_partial_and_total_failures() -> None:
    partial = KeywordAnnouncementSearchService(
        keyword_config(), MemoryAnnouncementRepository(), SearchHttpClient(failed_types={"com"})
    ).ingest("電科系")

    class LandingFailureHttpClient(SearchHttpClient):
        def get(self, url: str) -> str:
            raise RuntimeError("landing unavailable")

    failed = KeywordAnnouncementSearchService(
        keyword_config(), MemoryAnnouncementRepository(), LandingFailureHttpClient()
    ).ingest("電科系")

    assert partial.warning == PARTIAL_SEARCH_FAILURE_WARNING
    assert partial.summary.created == 1
    assert partial.canonical_urls == (
        "https://csai.nptu.edu.tw/p/406-1096-197001.php?Lang=zh-tw",
    )
    assert failed.warning == FULL_SEARCH_FAILURE_WARNING
    assert failed.summary.failed == 1
    assert failed.canonical_urls is None
    assert "hidden" not in " ".join(failed.summary.errors).lower()


def test_keyword_search_service_returns_empty_scope_for_successful_empty_search() -> None:
    class EmptySearchHttpClient(SearchHttpClient):
        def submit_form(self, method: str, url: str, fields: dict[str, str]) -> str:
            if "Action=mobileloadmod" in url:
                return BOOTSTRAP_FIXTURE.read_text(encoding="utf-8")
            return FORM_FIXTURE.read_text(encoding="utf-8") + '<div data-search-results></div>'

    result = KeywordAnnouncementSearchService(
        keyword_config(aliases={}),
        MemoryAnnouncementRepository(),
        EmptySearchHttpClient(),
    ).ingest("查無結果關鍵字")

    assert result.canonical_urls == ()
    assert result.warning is None


def test_keyword_search_service_limits_unique_results_before_detail_fetch() -> None:
    rows = "".join(
        f'<tr><td class="date">2026-07-{index:02d}</td><td><a href="https://www.nptu.edu.tw/p/406-1000-{index}.php">公告 {index}</a></td><td class="unit">測試單位</td></tr>'
        for index in range(1, 26)
    )

    class ManyResultsHttpClient(SearchHttpClient):
        def submit_form(self, method: str, url: str, fields: dict[str, str]) -> str:
            if "Action=mobileloadmod" in url:
                return BOOTSTRAP_FIXTURE.read_text(encoding="utf-8")
            self.submissions.append((fields["SchKey"], fields["SchType"]))
            return FORM_FIXTURE.read_text(encoding="utf-8") + f'<div data-search-results><table>{rows}</table></div>'

    repository = MemoryAnnouncementRepository()
    result = KeywordAnnouncementSearchService(
        keyword_config(max_items=20, aliases={}), repository, ManyResultsHttpClient()
    ).ingest("人工智慧")

    assert result.summary.created == 20
    assert len(repository.candidates) == 20
    assert repository.candidates[0].published_at == date(2026, 7, 25)
    assert repository.candidates[-1].published_at == date(2026, 7, 6)
