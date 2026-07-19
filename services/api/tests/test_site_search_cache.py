from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta, timezone
import json
import re
import threading
import time

import pytest

from nptu_assistant.crawlers.adapters.nptu_site import NptuListingItem, NptuSitePage
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.site_models import (
    DiscoveredPage,
    SearchDiagnostics,
    SearchDeadline,
    SearchPlan,
)
from nptu_assistant.crawlers.official_units import DocumentSearchScope
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    SiteSearchResult,
    site_search_cache_key,
)
from nptu_assistant.crawlers.site_search_cache import (
    InMemorySiteSearchCache,
    LayeredSiteSearchCache,
    PostgresSiteSearchCache,
    SiteSearchCacheEntry,
    SingleFlightCoordinator,
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

    entry = cache.get("key")
    assert entry is not None
    assert entry.result == result
    now[0] = 1.1
    assert cache.get("key") is None


class _FixedEntryCache:
    def __init__(self, entry: SiteSearchCacheEntry) -> None:
        self.entry = entry

    def get(self, cache_key: str) -> SiteSearchCacheEntry:
        del cache_key
        return self.entry

    def set(self, cache_key: str, result: SiteSearchResult, ttl_seconds: float) -> None:
        del cache_key, result, ttl_seconds


def test_layered_cache_does_not_extend_l2_remaining_ttl() -> None:
    now = [0.0]
    result = _result()
    l1 = InMemorySiteSearchCache(clock=lambda: now[0])
    l2 = _FixedEntryCache(
        SiteSearchCacheEntry(result=result, remaining_ttl_seconds=1.0)
    )
    cache = LayeredSiteSearchCache(l1, l2, ttl_seconds=300)

    entry = cache.get("key")
    assert entry is not None
    assert entry.result == result
    now[0] = 0.99
    assert l1.get("key") is not None
    now[0] = 1.01
    assert l1.get("key") is None


def _cache_key_service() -> NptuSiteSearchService:
    config = SiteSearchConfig(
        enabled=True,
        allowed_hosts=["nptu.edu.tw"],
        seed_urls=["https://www.nptu.edu.tw/"],
        cache_ttl_seconds=300,
    )
    return NptuSiteSearchService(config, object())  # type: ignore[arg-type]


def _large_search_plan() -> SearchPlan:
    query = "甲乙丙丁戊己庚辛壬癸" * 50
    variants = [f"變體{index}" + "子丑寅卯" * 49 for index in range(4)]
    concepts = [f"概念{index}" + "春夏秋冬" * 19 for index in range(8)]
    return SearchPlan(
        query=query,
        search_queries=variants,
        concepts=concepts,
        limit=20,
    )


def test_site_search_cache_key_is_stable_sha256_and_scope_sensitive() -> None:
    service = _cache_key_service()
    search_plan = _large_search_plan()
    scope = DocumentSearchScope(
        canonical_unit="測試單位",
        homepage_url="https://unit.nptu.edu.tw/",
        preferred_hosts=("unit.nptu.edu.tw", "www.nptu.edu.tw"),
        allowed_hosts=("unit.nptu.edu.tw", "www.nptu.edu.tw", "admission.nptu.edu.tw"),
        seed_urls=(
            "https://unit.nptu.edu.tw/",
            "https://unit.nptu.edu.tw/news",
            "https://admission.nptu.edu.tw/",
        ),
    )
    payload = service._cache_payload(
        search_plan,
        limit=20,
        use_discovery=True,
        scope=scope,
    )
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    key = site_search_cache_key(payload)

    assert len(search_plan.query) == 500
    assert len(search_plan.search_queries) == 4
    assert all(len(value) <= 200 for value in search_plan.search_queries)
    assert len(search_plan.concepts) == 8
    assert all(len(value) <= 80 for value in search_plan.concepts)
    assert len(canonical_json.encode("utf-8")) > 4_096
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert search_plan.query not in key
    assert key == service._cache_key(
        search_plan,
        limit=20,
        use_discovery=True,
        scope=scope,
    )
    assert key == site_search_cache_key(dict(reversed(list(payload.items()))))
    changed_payload = dict(payload)
    changed_payload["concepts"] = [*search_plan.concepts[:-1], "不同概念"]
    assert site_search_cache_key(changed_payload) != key
    changed_scope = dict(payload)
    changed_scope["allowed_hosts"] = ["other.nptu.edu.tw"]
    assert site_search_cache_key(changed_scope) != key


class _CountingMissCache:
    def __init__(self) -> None:
        self.get_calls = 0

    def get(self, cache_key: str) -> None:
        del cache_key
        self.get_calls += 1
        return None

    def set(self, cache_key: str, result: SiteSearchResult, ttl_seconds: float) -> None:
        del cache_key, result, ttl_seconds


class _NeverAcquire:
    def __init__(self) -> None:
        self.acquire_calls = 0

    def acquire(self, cache_key: str) -> None:
        del cache_key
        self.acquire_calls += 1
        return None


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_single_flight_waiter_uses_bounded_exponential_backoff() -> None:
    clock = _Clock()
    delays: list[float] = []
    cache = _CountingMissCache()
    single_flight = _NeverAcquire()

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        clock.value += seconds

    config = SiteSearchConfig(
        enabled=True,
        allowed_hosts=["nptu.edu.tw"],
        seed_urls=["https://www.nptu.edu.tw/"],
        cache_ttl_seconds=300,
        query_timeout_seconds=1,
    )
    service = NptuSiteSearchService(
        config,
        object(),  # type: ignore[arg-type]
        cache=cache,  # type: ignore[arg-type]
        single_flight=single_flight,  # type: ignore[arg-type]
        clock=clock,
        sleep=sleep,
    )

    result = service.search(
        "等待測試",
        deadline=SearchDeadline.after(1, clock=clock),
    )

    assert result.diagnostics.query_timed_out
    assert delays == pytest.approx([0.05, 0.1, 0.2, 0.4, 0.25])
    assert cache.get_calls == 6
    assert single_flight.acquire_calls == 6
    assert len(delays) < 20


class _InvalidRecord:
    payload = {"invalid": True}
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)


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


class _RecordSession:
    def __init__(self, record: object) -> None:
        self.record = record

    def __enter__(self) -> "_RecordSession":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def scalar(self, statement: object) -> object:
        del statement
        return self.record


class _RecordFactory:
    def __init__(self, record: object) -> None:
        self.record = record

    def __call__(self) -> _RecordSession:
        return _RecordSession(self.record)


def test_postgres_cache_entry_reports_utc_remaining_ttl() -> None:
    record = type(
        "Record",
        (),
        {
            "payload": serialize_site_search_result(_result()),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=1),
        },
    )()
    cache = PostgresSiteSearchCache(_RecordFactory(record))  # type: ignore[arg-type]

    entry = cache.get("key")

    assert entry is not None
    assert entry.expires_at is not None
    assert entry.expires_at.tzinfo == timezone.utc
    assert entry.remaining_ttl_seconds is not None
    assert 0.0 < entry.remaining_ttl_seconds <= 1.0


def test_postgres_cache_entry_with_expired_timestamp_is_a_miss() -> None:
    record = type(
        "Record",
        (),
        {
            "payload": serialize_site_search_result(_result()),
            "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
    )()
    cache = PostgresSiteSearchCache(_RecordFactory(record))  # type: ignore[arg-type]

    assert cache.get("expired") is None


class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _AdvisorySession:
    def __init__(
        self,
        *,
        bind_error: BaseException | None = None,
        scalar_error: BaseException | None = None,
        scalar_error_on_call: int | None = None,
        scalar_values: list[bool] | None = None,
    ) -> None:
        self.bind_error = bind_error
        self.scalar_error = scalar_error
        self.scalar_error_on_call = scalar_error_on_call
        self.scalar_values = list(scalar_values or [])
        self.scalar_calls = 0
        self.close_calls = 0

    def get_bind(self) -> _FakeBind:
        if self.bind_error is not None:
            raise self.bind_error
        return _FakeBind()

    def scalar(self, statement: object) -> bool:
        del statement
        self.scalar_calls += 1
        if self.scalar_error is not None and (
            self.scalar_error_on_call is None
            or self.scalar_calls == self.scalar_error_on_call
        ):
            raise self.scalar_error
        return self.scalar_values.pop(0)

    def close(self) -> None:
        self.close_calls += 1


class _OneSessionFactory:
    def __init__(self, session: _AdvisorySession) -> None:
        self.session = session

    def __call__(self) -> _AdvisorySession:
        return self.session


@pytest.mark.parametrize(
    "session",
    [
        _AdvisorySession(bind_error=RuntimeError("bind failed")),
        _AdvisorySession(scalar_error=RuntimeError("scalar failed")),
    ],
)
def test_advisory_lock_acquire_exception_closes_session(
    session: _AdvisorySession,
) -> None:
    coordinator = SingleFlightCoordinator(_OneSessionFactory(session))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        coordinator.try_acquire("exception-cleanup")

    assert session.close_calls == 1


def test_advisory_lock_unsuccessful_acquire_closes_session() -> None:
    session = _AdvisorySession(scalar_values=[False])
    coordinator = SingleFlightCoordinator(_OneSessionFactory(session))  # type: ignore[arg-type]

    assert coordinator.try_acquire("lock-held") is None
    assert session.close_calls == 1


def test_advisory_lock_lease_release_is_idempotent_and_closes_session() -> None:
    session = _AdvisorySession(scalar_values=[True, True])
    coordinator = SingleFlightCoordinator(_OneSessionFactory(session))  # type: ignore[arg-type]

    lease = coordinator.try_acquire("release-once")
    assert lease is not None
    assert session.close_calls == 0

    lease.release()
    lease.release()

    assert session.scalar_calls == 2
    assert session.close_calls == 1


def test_advisory_lock_unlock_exception_still_closes_session() -> None:
    session = _AdvisorySession(
        scalar_values=[True, True],
        scalar_error=RuntimeError("unlock failed"),
        scalar_error_on_call=2,
    )
    coordinator = SingleFlightCoordinator(_OneSessionFactory(session))  # type: ignore[arg-type]

    lease = coordinator.try_acquire("unlock-error")
    assert lease is not None
    lease.release()

    assert session.close_calls == 1


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
