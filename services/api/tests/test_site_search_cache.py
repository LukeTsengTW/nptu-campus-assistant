from __future__ import annotations

from collections.abc import Collection
import threading
import time

from nptu_assistant.crawlers.adapters.nptu_site import NptuListingItem, NptuSitePage
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.site_models import (
    DiscoveredPage,
    SearchDiagnostics,
    SearchDeadline,
)
from nptu_assistant.crawlers.site_search import NptuSiteSearchService, SiteSearchResult
from nptu_assistant.crawlers.site_search_cache import (
    InMemorySiteSearchCache,
    PostgresSiteSearchCache,
    SingleFlightSearchRunner,
    deserialize_site_search_result,
    serialize_site_search_result,
)


def _result() -> SiteSearchResult:
    page = NptuSitePage(
        title="校務資訊",
        canonical_url="https://www.nptu.edu.tw/guide",
        body="完整內容",
        published_at=None,
        links=("https://www.nptu.edu.tw/next",),
        link_texts=(("https://www.nptu.edu.tw/next", "下一頁"),),
        headings=("校務資訊",),
        score=0.8,
        announcement_items=(
            NptuListingItem(
                title="公告",
                canonical_url="https://www.nptu.edu.tw/announcement",
                published_at=None,
                summary="摘要",
                anchor_text="公告",
                order=0,
            ),
        ),
    )
    return SiteSearchResult((page,), SearchDiagnostics(fetched_count=1))


def test_site_search_result_cache_payload_is_safe_and_round_trips() -> None:
    result = _result()

    payload = serialize_site_search_result(result)
    restored = deserialize_site_search_result(payload)

    assert restored == result


def test_in_memory_site_search_cache_expires() -> None:
    now = [0.0]
    cache = InMemorySiteSearchCache(clock=lambda: now[0])
    result = _result()
    cache.set("key", result, 1)

    assert cache.get("key") == result
    now[0] = 1.1
    assert cache.get("key") is None


class _InvalidRecord:
    payload = {"invalid": True}


class _InvalidSession:
    def __enter__(self) -> "_InvalidSession":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def scalar(self, statement: object) -> _InvalidRecord:
        del statement
        return _InvalidRecord()


class _InvalidFactory:
    def __call__(self) -> _InvalidSession:
        return _InvalidSession()


class _BrokenFactory:
    def __call__(self) -> object:
        raise RuntimeError("cache backend unavailable")


def test_postgres_cache_treats_invalid_payload_and_backend_failure_as_miss() -> None:
    invalid = PostgresSiteSearchCache(_InvalidFactory())  # type: ignore[arg-type]
    broken = PostgresSiteSearchCache(_BrokenFactory())  # type: ignore[arg-type]

    assert invalid.get("bad-payload") is None
    assert broken.get("backend-error") is None
    broken.set("backend-error", _result(), 60)


class _Factory:
    def __call__(self) -> object:
        return object()


class _Discovery:
    def __init__(self) -> None:
        self.calls = 0
        self._guard = threading.Lock()

    def discover(
        self,
        plan: object,
        *,
        max_items: int,
        deadline: SearchDeadline,
    ) -> tuple[DiscoveredPage, ...]:
        del plan, max_items
        deadline.raise_if_expired()
        with self._guard:
            self.calls += 1
        time.sleep(0.08)
        return (DiscoveredPage("https://www.nptu.edu.tw/guide", "校務資訊", 1.0),)


class _Http:
    def __init__(self) -> None:
        self.calls = 0
        self._guard = threading.Lock()

    def get(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        del url, allowed_hosts, timeout_seconds
        deadline.raise_if_expired()
        with self._guard:
            self.calls += 1
        return "<main><h1>校務資訊</h1><p>完整校務資訊內容。</p></main>"


def test_single_flight_deduplicates_five_concurrent_live_searches() -> None:
    config = SiteSearchConfig(
        enabled=True,
        seed_urls=["https://www.nptu.edu.tw/"],
        allowed_hosts=["nptu.edu.tw"],
        max_pages=2,
        max_items=2,
        max_candidate_urls=8,
        cache_ttl_seconds=300,
        query_timeout_seconds=5,
    )
    discovery = _Discovery()
    http = _Http()
    service = NptuSiteSearchService(
        config,
        http,
        discovery=discovery,
        single_flight=SingleFlightSearchRunner(_Factory()),
    )
    results: list[SiteSearchResult] = []

    threads = [
        threading.Thread(target=lambda: results.append(service.search("校務資訊")))
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 5
    assert discovery.calls == 1
    assert http.calls == 2
    assert all(item == results[0] for item in results)
