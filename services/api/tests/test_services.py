from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.service import CrawlerService
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.service import DocumentIngestionService
from nptu_assistant.providers.fake import FakeEmbeddingProvider
from nptu_assistant.api.services import HealthService
from nptu_assistant.core.settings import Settings
from nptu_assistant.wiring import build_services


class HealthySession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def execute(self, statement):
        del statement
        return self

    def scalar_one(self) -> int:
        return 1


class HealthyFactory:
    def __call__(self) -> HealthySession:
        return HealthySession()


class FailingFactory:
    def __call__(self):
        raise RuntimeError("database unavailable")


def test_health_service_reports_ok_degraded_and_unhealthy() -> None:
    fake_settings = Settings(
        _env_file=None,
        llm_provider="fake",
        embedding_provider="fake",
        openai_api_key=None,
    )
    openai_without_key = Settings(
        _env_file=None,
        llm_provider="openai",
        embedding_provider="openai",
        openai_api_key=None,
    )

    assert HealthService(HealthyFactory(), fake_settings).check()["status"] == "ok"
    assert (
        HealthService(HealthyFactory(), openai_without_key).check()["status"]
        == "degraded"
    )
    assert (
        HealthService(FailingFactory(), fake_settings).check()["status"] == "unhealthy"
    )


def test_wiring_shares_one_openai_client_between_text_and_embeddings(
    monkeypatch,
) -> None:
    clients: list[object] = []

    class FakeOpenAIClient:
        pass

    def create_client(*, api_key: str) -> object:
        assert api_key == "test-key"
        client = FakeOpenAIClient()
        clients.append(client)
        return client

    monkeypatch.setattr("nptu_assistant.wiring.OpenAI", create_client)
    services = build_services(
        Settings(
            _env_file=None,
            openai_api_key="test-key",
            llm_provider="openai",
            embedding_provider="openai",
        )
    )
    chat_service = services["chat_service"]

    assert len(clients) == 1
    assert chat_service._llm._client is clients[0]
    assert (
        chat_service._tool_executor._retriever._embedding_provider._client is clients[0]
    )
    keyword_ingestor = chat_service._tool_executor._keyword_ingestor
    crawler_service = services["crawler_service"]
    assert keyword_ingestor is not None
    assert keyword_ingestor._http is crawler_service._http
    assert keyword_ingestor._repository is crawler_service._repository
    assert keyword_ingestor.normalize("電科系") == "電腦科學與人工智慧學系"
    assert keyword_ingestor._site_searcher is not None
    assert keyword_ingestor._site_searcher.config.allowed_hosts == ["nptu.edu.tw"]
    assert chat_service._tool_executor._site_page_ingestor is not None


class MemoryDocumentRepository:
    def __init__(self) -> None:
        self.hashes: set[tuple[str, str]] = set()

    def has_hash(self, canonical_url: str, digest: str) -> bool:
        return (canonical_url, digest) in self.hashes

    def save(self, metadata, raw_text, chunks, embeddings) -> None:
        assert len(chunks) == len(embeddings)
        self.hashes.add((str(metadata.source_url), content_hash(raw_text)))


class MemoryAnnouncementRepository:
    def __init__(self) -> None:
        self.urls: set[str] = set()
        self.candidates: list[AnnouncementCandidate] = []
        self.source_urls: list[str] = []

    def upsert(
        self,
        candidate: AnnouncementCandidate,
        *,
        source_name: str,
        source_url: str,
        interval_minutes: int,
    ) -> str:
        del source_name, interval_minutes
        self.candidates.append(candidate)
        self.source_urls.append(source_url)
        if candidate.canonical_url in self.urls:
            return "unchanged"
        self.urls.add(candidate.canonical_url)
        return "created"

    def commit_source_refresh(
        self,
        candidates: list[AnnouncementCandidate],
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
        crawled_at: object,
    ) -> list[str]:
        del source_unit, crawled_at
        return [
            self.upsert(
                candidate,
                source_name=source_name,
                source_url=source_url,
                interval_minutes=interval_minutes,
            )
            for candidate in candidates
        ]


class UnusedHttpClient:
    def get(self, url: str) -> str:
        raise AssertionError(f"fixture 不應發出 HTTP request: {url}")


class FeedWithFailingDetailHttpClient:
    def get(self, url: str) -> str:
        if url.endswith("feed.xml"):
            return """<?xml version="1.0"?><rss><channel><item>
            <title>測試公告</title><link>https://www.nptu.edu.tw/detail</link>
            <description><![CDATA[<p>摘要內容</p>]]></description>
            <pubDate>2026-07-10</pubDate><author>測試單位</author>
            </item></channel></rss>"""
        raise RuntimeError("detail unavailable")


def test_document_ingestion_creates_then_skips_same_content(tmp_path: Path) -> None:
    document = tmp_path / "rule.md"
    document.write_text("# 測試辦法\n\n第一條　測試內容。", encoding="utf-8")
    document.with_suffix(".yaml").write_text(
        "\n".join(
            [
                "title: 測試辦法",
                "source_url: https://www.nptu.edu.tw/rule",
                "unit: 教務處",
                "published_at: 2026-01-01",
                "document_type: regulation",
                'version: "1.0"',
            ]
        ),
        encoding="utf-8",
    )
    repository = MemoryDocumentRepository()
    service = DocumentIngestionService(
        tmp_path, repository, FakeEmbeddingProvider(1536)
    )

    first = service.run()
    second = service.run()

    assert first.created == 1
    assert first.failed == 0
    assert second.skipped == 1


def test_fixture_crawler_creates_then_reports_unchanged(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "data/fixtures/announcements"
    fixture_dir.mkdir(parents=True)
    fixture_dir.joinpath("overview.xml").write_text(
        """<?xml version="1.0"?><rss><channel><item>
        <title>測試公告</title><link>https://www.nptu.edu.tw/a</link>
        <description><![CDATA[<p>內容</p>]]></description>
        <pubDate>2026-07-10</pubDate><author>教務處</author>
        </item></channel></rss>""",
        encoding="utf-8",
    )
    fixture_dir.joinpath("detail.html").write_text(
        "<main><h1>測試公告</h1><p>完整內容</p></main>", encoding="utf-8"
    )
    config = tmp_path / "sources.yaml"
    config.write_text(
        """sources:
  - name: fixture
    adapter: fixture
    url: data/fixtures/announcements/overview.xml
    unit: 測試
    enabled: false
    crawl_interval_minutes: 60
""",
        encoding="utf-8",
    )
    repository = MemoryAnnouncementRepository()
    service = CrawlerService(
        config,
        repository,
        UnusedHttpClient(),
        workspace_root=tmp_path,
    )

    first = service.run(["fixture"])
    second = service.run(["fixture"])

    assert first.created == 1
    assert second.unchanged == 1
    assert repository.source_urls[0].startswith("https://")


def test_empty_fixture_listing_is_a_successful_empty_source_scope(
    tmp_path: Path,
) -> None:
    fixture_dir = tmp_path / "data/fixtures/announcements"
    fixture_dir.mkdir(parents=True)
    fixture_dir.joinpath("overview.xml").write_text(
        "<?xml version='1.0'?><rss><channel></channel></rss>",
        encoding="utf-8",
    )
    config = tmp_path / "sources.yaml"
    config.write_text(
        """sources:
  - name: empty-fixture
    adapter: fixture
    url: data/fixtures/announcements/overview.xml
    unit: 測試單位
    enabled: false
""",
        encoding="utf-8",
    )
    service = CrawlerService(
        config,
        MemoryAnnouncementRepository(),
        UnusedHttpClient(),
        workspace_root=tmp_path,
    )

    result = service.run_with_urls(["empty-fixture"])

    assert result.summary.failed == 0
    assert result.canonical_urls == {"empty-fixture": ()}


def test_http_client_checks_robots_and_retries_twice() -> None:
    attempts = {"listing": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        attempts["listing"] += 1
        status = 503 if attempts["listing"] < 3 else 200
        return httpx.Response(status, text="ok", request=request)

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.get("https://www.nptu.edu.tw/list") == "ok"
    finally:
        client.close()

    assert attempts["listing"] == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval_seconds": -1},
        {"max_response_bytes": 0},
        {"max_redirects": -1},
        {"timeout_seconds": 0},
    ],
)
def test_http_client_rejects_invalid_resource_limits(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        CrawlHttpClient("NPTU-Test/1.0", **kwargs)


def test_http_client_rechecks_robots_after_reset() -> None:
    requests = {"robots": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            requests["robots"] += 1
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(200, text="ok", request=request)

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        client.get("https://www.nptu.edu.tw/list")
        client.reset_robots()
        client.get("https://www.nptu.edu.tw/list")
    finally:
        client.close()

    assert requests["robots"] == 2


def test_http_client_rejects_redirect_outside_nptu_allowlist() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(str(request.url.host))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if request.url.host == "www.nptu.edu.tw":
            return httpx.Response(
                302,
                headers={"Location": "https://example.com/content"},
                request=request,
            )
        return httpx.Response(200, text="external", request=request)

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="allowlist"):
            client.get("https://www.nptu.edu.tw/list")
    finally:
        client.close()

    assert "example.com" not in requested_hosts


def test_http_client_get_html_rejects_non_html_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(
            200,
            content=b"%PDF-1.7",
            headers={"Content-Type": "application/pdf"},
            request=request,
        )

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="HTML"):
            client.get_html("https://www.nptu.edu.tw/file")
    finally:
        client.close()


def test_http_client_rejects_redirect_outside_source_host_before_request() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(str(request.url.host))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(
            302,
            headers={"Location": "https://www.nptu.edu.tw/content"},
            request=request,
        )

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="allowlist"):
            client.get(
                "https://ccs.nptu.edu.tw/list",
                allowed_hosts=["ccs.nptu.edu.tw"],
            )
    finally:
        client.close()

    assert "www.nptu.edu.tw" not in requested_hosts


def test_http_client_allows_validated_redirect_and_checks_target_robots() -> None:
    robots_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            robots_hosts.append(str(request.url.host))
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if request.url.host == "ccs.nptu.edu.tw":
            return httpx.Response(
                302,
                headers={"Location": "https://news.ccs.nptu.edu.tw/content"},
                request=request,
            )
        return httpx.Response(200, text="redirected", request=request)

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (
            client.get(
                "https://ccs.nptu.edu.tw/list",
                allowed_hosts=["ccs.nptu.edu.tw"],
            )
            == "redirected"
        )
    finally:
        client.close()

    assert robots_hosts == ["ccs.nptu.edu.tw", "news.ccs.nptu.edu.tw"]


def test_http_client_get_form_redirect_uses_location_query_without_reapplying_fields() -> (
    None
):
    requested_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        requested_queries.append(str(request.url.query))
        if request.url.path == "/search":
            return httpx.Response(
                302,
                headers={"Location": "/results?token=abc"},
                request=request,
            )
        return httpx.Response(200, text="redirected", request=request)

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.submit_form(
            "get",
            "https://www.nptu.edu.tw/search",
            {"SchKey": "test"},
        )
    finally:
        client.close()

    assert result == "redirected"
    assert requested_queries == ["b'SchKey=test'", "b'token=abc'"]


def test_http_client_rejects_oversized_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(200, content=b"12345", request=request)

    client = CrawlHttpClient(
        "NPTU-Test/1.0",
        interval_seconds=0,
        max_response_bytes=4,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="大小上限"):
            client.get("https://www.nptu.edu.tw/list")
    finally:
        client.close()


def test_crawler_preserves_feed_description_and_records_detail_warning(
    tmp_path: Path,
) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        """sources:
  - name: live-fixture
    adapter: nptu_overview
    url: https://www.nptu.edu.tw/feed.xml
    unit: 測試單位
    enabled: true
    crawl_interval_minutes: 60
""",
        encoding="utf-8",
    )
    repository = MemoryAnnouncementRepository()
    service = CrawlerService(
        config,
        repository,
        FeedWithFailingDetailHttpClient(),
        workspace_root=tmp_path,
    )

    summary = service.run()

    assert summary.created == 1
    assert repository.candidates[0].body == "摘要內容"
    assert repository.candidates[0].warning is not None
