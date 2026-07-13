from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.config import CrawlerSourceConfig, load_source_configs
from nptu_assistant.crawlers.service import CrawlRunResult


REFRESH_FAILURE_WARNING = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
logger = logging.getLogger(__name__)


class CrawlRunner(Protocol):
    def run_with_urls(self, source_names: list[str] | None = None) -> CrawlRunResult:
        raise NotImplementedError


class FreshnessRepository(Protocol):
    def latest_crawled_at(self, source_name: str) -> datetime | None:
        raise NotImplementedError

    def canonical_urls_for_source(self, source_name: str) -> tuple[str, ...] | None:
        raise NotImplementedError

    def record_source_refresh(
        self,
        *,
        source_name: str,
        source_url: str,
        unit: str,
        interval_minutes: int,
        canonical_urls: tuple[str, ...],
        crawled_at: datetime,
    ) -> None:
        raise NotImplementedError


class DueSourceRefresher(Protocol):
    def refresh_due_sources(self) -> list[RefreshResult]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class RefreshResult:
    source_name: str
    attempted: bool
    succeeded: bool
    warning: str | None = None
    summary: CrawlSummary | None = None
    canonical_urls: tuple[str, ...] | None = None


class AnnouncementRefreshCoordinator:
    def __init__(
        self,
        config_path: Path,
        crawler: CrawlRunner,
        repository: FreshnessRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._config_path = config_path
        self._crawler = crawler
        self._repository = repository
        self._now = now
        self._lock = threading.Lock()
        self._last_success: dict[str, datetime] = {}

    def ensure_fresh(self, source_name: str) -> RefreshResult:
        config = self._config(source_name)
        with self._lock:
            checked_at = self._now()
            if not self._is_due(config, checked_at):
                return RefreshResult(
                    source_name,
                    attempted=False,
                    succeeded=True,
                    canonical_urls=self._repository.canonical_urls_for_source(source_name),
                )
            run_result = self._crawler.run_with_urls([source_name])
            summary = run_result.summary
            canonical_urls = run_result.canonical_urls.get(source_name)
            if summary.failed or canonical_urls is None:
                return RefreshResult(
                    source_name,
                    attempted=True,
                    succeeded=False,
                    warning=REFRESH_FAILURE_WARNING,
                    summary=summary,
                    canonical_urls=self._repository.canonical_urls_for_source(source_name),
                )
            if source_name not in run_result.persisted_source_snapshots:
                self._repository.record_source_refresh(
                    source_name=config.name,
                    source_url=config.url,
                    unit=config.unit,
                    interval_minutes=config.crawl_interval_minutes,
                    canonical_urls=canonical_urls,
                    crawled_at=checked_at,
                )
            self._last_success[source_name] = checked_at
            return RefreshResult(
                source_name,
                attempted=True,
                succeeded=True,
                summary=summary,
                canonical_urls=canonical_urls,
            )

    def refresh_due_sources(self) -> list[RefreshResult]:
        return [
            self.ensure_fresh(config.name)
            for config in load_source_configs(self._config_path)
            if config.enabled
        ]

    def _config(self, source_name: str) -> CrawlerSourceConfig:
        configs = {item.name: item for item in load_source_configs(self._config_path)}
        config = configs.get(source_name)
        if config is None or not config.enabled:
            raise ValueError(f"未知或未啟用的 crawler source：{source_name}")
        return config

    def _is_due(self, config: CrawlerSourceConfig, checked_at: datetime) -> bool:
        timestamps = [
            value
            for value in (
                self._repository.latest_crawled_at(config.name),
                self._last_success.get(config.name),
            )
            if value is not None
        ]
        if not timestamps:
            return True
        return checked_at - max(timestamps) >= timedelta(
            minutes=config.crawl_interval_minutes
        )


class AnnouncementRefreshScheduler:
    def __init__(
        self,
        coordinator: DueSourceRefresher,
        *,
        check_interval_seconds: float = 60.0,
    ) -> None:
        self._coordinator = coordinator
        self._check_interval_seconds = check_interval_seconds
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._coordinator.refresh_due_sources)
            except Exception:
                logger.exception("announcement_refresh_scheduler_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._check_interval_seconds,
                )
            except TimeoutError:
                continue
