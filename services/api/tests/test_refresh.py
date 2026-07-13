from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.refresh import (
    AnnouncementRefreshCoordinator,
    AnnouncementRefreshScheduler,
    RefreshResult,
)
from nptu_assistant.crawlers.service import CrawlRunResult


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class MemoryFreshnessRepository:
    def __init__(
        self,
        last_crawled_at: datetime | None,
        canonical_urls: tuple[str, ...] | None = None,
    ) -> None:
        self.last_crawled_at_value = last_crawled_at
        self.canonical_urls_value = canonical_urls
        self.refreshes: list[dict[str, object]] = []

    def latest_crawled_at(self, source_name: str) -> datetime | None:
        assert source_name == "nptu-overview"
        return self.last_crawled_at_value

    def canonical_urls_for_source(self, source_name: str) -> tuple[str, ...] | None:
        assert source_name == "nptu-overview"
        return self.canonical_urls_value

    def record_source_refresh(self, **values: object) -> None:
        self.refreshes.append(values)
        self.last_crawled_at_value = values["crawled_at"]  # type: ignore[assignment]
        self.canonical_urls_value = values["canonical_urls"]  # type: ignore[assignment]


class RecordingCrawler:
    def __init__(
        self,
        summary: CrawlSummary | None = None,
        canonical_urls: tuple[str, ...] = (
            "https://www.nptu.edu.tw/p/406-1000-200001.php",
        ),
        *,
        persisted_source_snapshots: bool = False,
    ) -> None:
        self.summary = summary or CrawlSummary(unchanged=20)
        self.canonical_urls = canonical_urls
        self.persisted_source_snapshots = persisted_source_snapshots
        self.calls: list[list[str] | None] = []

    def run_with_urls(self, source_names: list[str] | None = None) -> CrawlRunResult:
        self.calls.append(source_names)
        names = source_names or ["nptu-overview"]
        return CrawlRunResult(
            self.summary,
            {name: self.canonical_urls for name in names} if not self.summary.failed else {},
            frozenset(names) if self.persisted_source_snapshots else frozenset(),
        )


class BlockingCrawler(RecordingCrawler):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run_with_urls(self, source_names: list[str] | None = None) -> CrawlRunResult:
        self.calls.append(source_names)
        self.started.set()
        assert self.release.wait(timeout=1)
        return CrawlRunResult(
            self.summary,
            {"nptu-overview": self.canonical_urls},
        )


class RecordingCoordinator:
    def __init__(self) -> None:
        self.calls = 0

    def refresh_due_sources(self) -> list[RefreshResult]:
        self.calls += 1
        return []


def write_config(path: Path) -> None:
    path.write_text(
        """sources:
  - name: nptu-overview
    adapter: nptu_overview
    url: https://www.nptu.edu.tw/p/503-1000-1044.php?Lang=zh-tw
    unit: 國立屏東大學
    category: 總覽
    enabled: true
    crawl_interval_minutes: 60
    max_items: 20
""",
        encoding="utf-8",
    )


def make_coordinator(
    config: Path,
    crawler: RecordingCrawler,
    last_crawled_at: datetime | None,
    canonical_urls: tuple[str, ...] | None = None,
) -> AnnouncementRefreshCoordinator:
    return AnnouncementRefreshCoordinator(
        config,
        crawler,
        MemoryFreshnessRepository(last_crawled_at, canonical_urls),
        now=lambda: NOW,
    )


def test_fresh_source_skips_crawl(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler()
    cached_urls = ("https://www.nptu.edu.tw/cached",)
    coordinator = make_coordinator(
        config,
        crawler,
        NOW - timedelta(minutes=59),
        cached_urls,
    )

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.attempted is False
    assert result.succeeded is True
    assert result.canonical_urls == cached_urls
    assert crawler.calls == []


def test_due_source_crawls_once_then_stays_fresh_in_memory(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler()
    coordinator = make_coordinator(config, crawler, NOW - timedelta(minutes=60))

    first = coordinator.ensure_fresh("nptu-overview")
    second = coordinator.ensure_fresh("nptu-overview")

    assert first.attempted is True
    assert first.succeeded is True
    assert first.canonical_urls == crawler.canonical_urls
    assert second.attempted is False
    assert second.canonical_urls == crawler.canonical_urls
    assert crawler.calls == [["nptu-overview"]]


def test_failed_refresh_returns_stable_warning(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler(CrawlSummary(failed=1, errors=["HTTP 503"]))
    cached_urls = ("https://www.nptu.edu.tw/last-success",)
    repository = MemoryFreshnessRepository(NOW - timedelta(minutes=61), cached_urls)
    coordinator = AnnouncementRefreshCoordinator(
        config,
        crawler,
        repository,
        now=lambda: NOW,
    )

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.succeeded is False
    assert result.warning == "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
    assert result.canonical_urls == cached_urls
    assert repository.refreshes == []


def test_successful_empty_refresh_persists_an_empty_source_snapshot(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler(CrawlSummary(), canonical_urls=())
    repository = MemoryFreshnessRepository(None, ("https://www.nptu.edu.tw/old",))
    coordinator = AnnouncementRefreshCoordinator(
        config,
        crawler,
        repository,
        now=lambda: NOW,
    )

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.succeeded is True
    assert result.canonical_urls == ()
    assert repository.canonical_urls_value == ()
    assert repository.last_crawled_at_value == NOW
    assert repository.refreshes[0]["unit"] == "國立屏東大學"


def test_coordinator_does_not_write_a_second_snapshot_after_atomic_crawler_commit(
    tmp_path: Path,
) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler(persisted_source_snapshots=True)
    repository = MemoryFreshnessRepository(None)
    coordinator = AnnouncementRefreshCoordinator(
        config,
        crawler,
        repository,
        now=lambda: NOW,
    )

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.succeeded is True
    assert result.canonical_urls == crawler.canonical_urls
    assert repository.refreshes == []


def test_parallel_refreshes_only_crawl_once(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = BlockingCrawler()
    coordinator = make_coordinator(config, crawler, None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(coordinator.ensure_fresh, "nptu-overview")
        assert crawler.started.wait(timeout=1)
        second = executor.submit(coordinator.ensure_fresh, "nptu-overview")
        crawler.release.set()

        assert first.result(timeout=1).attempted is True
        assert second.result(timeout=1).attempted is False

    assert crawler.calls == [["nptu-overview"]]


def test_scheduler_checks_immediately_and_stops_cleanly() -> None:
    async def exercise() -> None:
        coordinator = RecordingCoordinator()
        scheduler = AnnouncementRefreshScheduler(
            coordinator,
            check_interval_seconds=0.01,
        )
        task = asyncio.create_task(scheduler.run())
        deadline = asyncio.get_running_loop().time() + 1
        while coordinator.calls < 2:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("scheduler 未在期限內執行第二次檢查")
            await asyncio.sleep(0.005)
        scheduler.stop()
        await asyncio.wait_for(task, timeout=1)

        assert coordinator.calls >= 2

    asyncio.run(exercise())
