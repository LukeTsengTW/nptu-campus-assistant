from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.config import CrawlerSourceConfig, load_source_configs


REFRESH_FAILURE_WARNING = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"


class CrawlRunner(Protocol):
    def run(self, source_names: list[str] | None = None) -> CrawlSummary:
        raise NotImplementedError


class FreshnessRepository(Protocol):
    def latest_crawled_at(self, source_name: str) -> datetime | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class RefreshResult:
    source_name: str
    attempted: bool
    succeeded: bool
    warning: str | None = None
    summary: CrawlSummary | None = None


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
                return RefreshResult(source_name, attempted=False, succeeded=True)
            summary = self._crawler.run([source_name])
            if summary.failed:
                return RefreshResult(
                    source_name,
                    attempted=True,
                    succeeded=False,
                    warning=REFRESH_FAILURE_WARNING,
                    summary=summary,
                )
            self._last_success[source_name] = checked_at
            return RefreshResult(
                source_name,
                attempted=True,
                succeeded=True,
                summary=summary,
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
