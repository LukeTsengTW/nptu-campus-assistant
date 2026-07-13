from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.refresh import AnnouncementRefreshCoordinator


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class MemoryFreshnessRepository:
    def __init__(self, last_crawled_at: datetime | None) -> None:
        self.last_crawled_at_value = last_crawled_at

    def latest_crawled_at(self, source_name: str) -> datetime | None:
        assert source_name == "nptu-overview"
        return self.last_crawled_at_value


class RecordingCrawler:
    def __init__(self, summary: CrawlSummary | None = None) -> None:
        self.summary = summary or CrawlSummary(unchanged=20)
        self.calls: list[list[str] | None] = []

    def run(self, source_names: list[str] | None = None) -> CrawlSummary:
        self.calls.append(source_names)
        return self.summary


class BlockingCrawler(RecordingCrawler):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, source_names: list[str] | None = None) -> CrawlSummary:
        self.calls.append(source_names)
        self.started.set()
        assert self.release.wait(timeout=1)
        return self.summary


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
) -> AnnouncementRefreshCoordinator:
    return AnnouncementRefreshCoordinator(
        config,
        crawler,
        MemoryFreshnessRepository(last_crawled_at),
        now=lambda: NOW,
    )


def test_fresh_source_skips_crawl(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler()
    coordinator = make_coordinator(config, crawler, NOW - timedelta(minutes=59))

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.attempted is False
    assert result.succeeded is True
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
    assert second.attempted is False
    assert crawler.calls == [["nptu-overview"]]


def test_failed_refresh_returns_stable_warning(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler(CrawlSummary(failed=1, errors=["HTTP 503"]))
    coordinator = make_coordinator(config, crawler, None)

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.succeeded is False
    assert result.warning == "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"


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
