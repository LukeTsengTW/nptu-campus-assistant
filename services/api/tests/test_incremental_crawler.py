from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePageAdapter
from nptu_assistant.crawlers.crawl_ingestion import (
    CrawlIngestionResult,
    CrawlIngestionStatus,
)
from nptu_assistant.crawlers.http import CrawlHttpClient, CrawlHttpResponse
from nptu_assistant.crawlers.incremental_crawler import (
    IncrementalCrawler,
    IncrementalCrawlOutcome,
    IncrementalCrawlTarget,
)
from nptu_assistant.ingestion.cleaning import content_hash

URL = "https://www.nptu.edu.tw/news"
HTML = """<html><head><title>校務公告</title></head>
<body><main><h1>校務公告</h1><p>這是一則官方公告內容。</p>
<a href='/news/next'>下一則公告</a></main></body></html>"""


class FakeHttpClient:
    def __init__(self, response: CrawlHttpResponse | Exception) -> None:
        self.response = response
        self.headers: list[dict[str, str]] = []

    def get_response(self, url: str, *, allowed_hosts, request_headers=None):
        del url, allowed_hosts
        self.headers.append(dict(request_headers or {}))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class MemoryPageSink:
    def __init__(self) -> None:
        self.pages = []
        self.failures = []

    def record_fetched_page(self, page, **kwargs):
        self.pages.append((page, kwargs))

    def record_crawl_failure(self, canonical_url, **kwargs):
        self.failures.append((canonical_url, kwargs))


class MemoryStateStore:
    def __init__(self) -> None:
        self.successes = []

    def record_crawl_success(self, canonical_url, **kwargs):
        self.successes.append((canonical_url, kwargs))


class MemoryIngestion:
    def __init__(self, status: CrawlIngestionStatus) -> None:
        self.status = status
        self.pages = []

    def ingest_page(self, page, *, unit=None):
        self.pages.append((page, unit))
        return CrawlIngestionResult(page.canonical_url, self.status)


class Lease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class MemoryLeaseRunner:
    def __init__(self) -> None:
        self.leases = []

    def acquire(self, key: str):
        del key
        lease = Lease()
        self.leases.append(lease)
        return lease


@dataclass
class MemoryScheduler:
    targets: tuple[IncrementalCrawlTarget, ...]

    def __post_init__(self) -> None:
        self.claims = []
        self.completed = []

    def claim_due(self, *, limit: int, now):
        self.claims.append((limit, now))
        return self.targets

    def complete(self, result) -> None:
        self.completed.append(result)


def response(
    *,
    status_code: int = 200,
    url: str = URL,
    content: bytes = HTML.encode(),
    headers: dict[str, str] | None = None,
) -> CrawlHttpResponse:
    return CrawlHttpResponse(
        status_code=status_code,
        url=url,
        headers={"content-type": "text/html; charset=utf-8", **(headers or {})},
        content=content,
    )


def make_worker(http, sink, **kwargs) -> IncrementalCrawler:
    return IncrementalCrawler(
        http,
        sink,
        allowed_hosts=("nptu.edu.tw",),
        host_interval_seconds=0,
        **kwargs,
    )


def test_http_client_sends_conditional_headers_and_returns_304_metadata() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(
            304,
            headers={
                "ETag": '"server-v2"',
                "Last-Modified": "Wed, 01 Jul 2026 00:00:00 GMT",
            },
            request=request,
        )

    client = CrawlHttpClient(
        "NPTU-Incremental-Test/1.0",
        interval_seconds=0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.get_response(
            URL,
            allowed_hosts=("nptu.edu.tw",),
            request_headers={"If-None-Match": '"client-v1"'},
        )
    finally:
        client.close()

    assert result.status_code == 304
    assert result.url == URL
    assert result.headers["etag"] == '"server-v2"'
    listing_request = requests[-1]
    assert listing_request.headers["if-none-match"] == '"client-v1"'


def test_conditional_get_304_records_unchanged_and_releases_lease() -> None:
    http = FakeHttpClient(
        response(
            status_code=304,
            content=b"",
            headers={"etag": '"new"', "last-modified": "Wed, 01 Jul 2026 00:00:00 GMT"},
        )
    )
    sink = MemoryPageSink()
    state = MemoryStateStore()
    leases = MemoryLeaseRunner()
    worker = make_worker(http, sink, lease_runner=leases, state_store=state)

    result = worker.run_once(
        [
            IncrementalCrawlTarget(
                URL,
                ("nptu.edu.tw",),
                etag='"old"',
                last_modified="Tue, 30 Jun 2026 00:00:00 GMT",
                content_hash="known-hash",
                title="校務公告",
            )
        ]
    ).results[0]

    assert result.outcome is IncrementalCrawlOutcome.UNCHANGED
    assert result.status_code == 304
    assert http.headers == [
        {
            "If-None-Match": '"old"',
            "If-Modified-Since": "Tue, 30 Jun 2026 00:00:00 GMT",
        }
    ]
    assert state.successes[0][1]["content_hash"] == "known-hash"
    assert sink.pages == []
    assert leases.leases[0].released is True


def test_200_html_uses_existing_page_parser_and_reports_changed() -> None:
    http = FakeHttpClient(response(headers={"etag": '"v2"'}))
    sink = MemoryPageSink()
    worker = make_worker(http, sink)

    result = worker.run_once([IncrementalCrawlTarget(URL)]).results[0]

    assert result.outcome is IncrementalCrawlOutcome.CHANGED
    assert result.content_hash == content_hash(sink.pages[0][0].body)
    assert sink.pages[0][1]["etag"] == '"v2"'
    assert sink.pages[0][0].canonical_url == URL


def test_200_with_same_content_hash_reports_unchanged() -> None:
    page = NptuSitePageAdapter().parse_page(
        HTML,
        URL,
        allowed_hosts=("nptu.edu.tw",),
    )
    worker = make_worker(
        FakeHttpClient(response()),
        MemoryPageSink(),
    )

    result = worker.run_once(
        [IncrementalCrawlTarget(URL, content_hash=content_hash(page.body))]
    ).results[0]

    assert result.outcome is IncrementalCrawlOutcome.UNCHANGED


def test_changed_page_ingests_once_but_hash_unchanged_page_does_not() -> None:
    page = NptuSitePageAdapter().parse_page(
        HTML,
        URL,
        allowed_hosts=("nptu.edu.tw",),
    )
    ingestion = MemoryIngestion(CrawlIngestionStatus.CREATED)
    worker = make_worker(
        FakeHttpClient(response()),
        MemoryPageSink(),
        ingestion_service=ingestion,
    )

    changed = worker.run_once([IncrementalCrawlTarget(URL)]).results[0]
    unchanged = worker.run_once(
        [IncrementalCrawlTarget(URL, content_hash=content_hash(page.body))]
    ).results[0]

    assert changed.outcome is IncrementalCrawlOutcome.CHANGED
    assert changed.ingestion_performed is True
    assert unchanged.outcome is IncrementalCrawlOutcome.UNCHANGED
    assert unchanged.ingestion_performed is False
    assert len(ingestion.pages) == 1


def test_binary_and_unsupported_responses_are_excluded_without_parsing() -> None:
    sink = MemoryPageSink()
    binary_url = "https://www.nptu.edu.tw/files/rules.pdf"
    binary = (
        make_worker(
            FakeHttpClient(
                response(
                    url=binary_url,
                    headers={"content-type": "application/pdf"},
                )
            ),
            sink,
        )
        .run_once([IncrementalCrawlTarget(binary_url)])
        .results[0]
    )
    unsupported_url = "https://www.nptu.edu.tw/feed"
    unsupported = (
        make_worker(
            FakeHttpClient(
                response(
                    url=unsupported_url,
                    headers={"content-type": "application/xml"},
                )
            ),
            sink,
        )
        .run_once([IncrementalCrawlTarget(unsupported_url)])
        .results[0]
    )

    assert binary.outcome is IncrementalCrawlOutcome.BINARY
    assert unsupported.outcome is IncrementalCrawlOutcome.UNSUPPORTED
    assert len(sink.failures) == 2
    assert all(call[1]["status"].value == "excluded" for call in sink.failures)


def test_final_redirect_outside_allowlist_is_a_failure_before_persistence() -> None:
    sink = MemoryPageSink()
    result = (
        make_worker(
            FakeHttpClient(response(url="https://example.com/news")),
            sink,
        )
        .run_once([IncrementalCrawlTarget(URL)])
        .results[0]
    )

    assert result.outcome is IncrementalCrawlOutcome.FAILED
    assert sink.pages == []
    assert sink.failures[0][0] == URL


def test_scheduler_claim_is_bounded_and_completion_is_reported() -> None:
    scheduler = MemoryScheduler(
        tuple(
            IncrementalCrawlTarget(f"https://www.nptu.edu.tw/news/{index}")
            for index in range(3)
        )
    )
    worker = make_worker(
        FakeHttpClient(response()),
        MemoryPageSink(),
        scheduler=scheduler,
        max_concurrency=2,
    )

    result = worker.run_once()

    assert scheduler.claims[0][0] == 2
    assert len(result.results) == 3
    assert scheduler.completed == list(result.results)


def test_formal_scheduler_claim_lease_is_completed_with_changed_flag() -> None:
    class FormalScheduler:
        def __init__(self) -> None:
            self.claim_args = None
            self.completed = []

        def claim_due(self, *, owner, limit, lease_duration):
            self.claim_args = (owner, limit, lease_duration)
            return (SimpleNamespace(canonical_url=URL),)

        def complete(self, claim, *, changed):
            self.completed.append((claim, changed))
            return True

        def fail(self, claim, **kwargs):
            raise AssertionError((claim, kwargs))

    scheduler = FormalScheduler()
    worker = make_worker(
        FakeHttpClient(response()),
        MemoryPageSink(),
        scheduler=scheduler,
        worker_id="worker-c",
    )

    result = worker.run_once()

    assert result.results[0].outcome is IncrementalCrawlOutcome.CHANGED
    assert scheduler.claim_args[0:2] == ("worker-c", 4)
    assert scheduler.completed[0][1] is True


def test_worker_limits_active_fetches_to_configured_concurrency() -> None:
    active = 0
    max_active = 0
    guard = threading.Lock()

    class SlowHttpClient(FakeHttpClient):
        def get_response(self, url: str, *, allowed_hosts, request_headers=None):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with guard:
                active -= 1
            return response(url=url)

    worker = make_worker(
        SlowHttpClient(response()),
        MemoryPageSink(),
        max_concurrency=2,
    )

    result = worker.run_once(
        [
            IncrementalCrawlTarget(f"https://www.nptu.edu.tw/news/{index}")
            for index in range(5)
        ]
    )

    assert len(result.results) == 5
    assert max_active <= 2
