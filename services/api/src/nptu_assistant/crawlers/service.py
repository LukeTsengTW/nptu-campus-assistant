from __future__ import annotations

from pathlib import Path
from typing import Protocol

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.adapters.fixture import FixtureAdapter
from nptu_assistant.crawlers.adapters.nptu import NptuOverviewAdapter
from nptu_assistant.crawlers.config import CrawlerSourceConfig, load_source_configs
from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.models import AnnouncementCandidate


class AnnouncementRepository(Protocol):
    def upsert(
        self,
        candidate: AnnouncementCandidate,
        *,
        source_name: str,
        source_url: str,
        interval_minutes: int,
    ) -> str: ...


class CrawlerService:
    def __init__(
        self,
        config_path: Path,
        repository: AnnouncementRepository,
        http_client: CrawlHttpClient,
        *,
        workspace_root: Path,
    ) -> None:
        self._config_path = config_path
        self._repository = repository
        self._http = http_client
        self._workspace_root = workspace_root

    def run(self, source_names: list[str] | None = None) -> CrawlSummary:
        summary = CrawlSummary()
        reset_robots = getattr(self._http, "reset_robots", None)
        if callable(reset_robots):
            reset_robots()
        configs = load_source_configs(self._config_path)
        known = {config.name for config in configs}
        requested = set(source_names or [])
        unknown = requested - known
        if unknown:
            summary.failed += len(unknown)
            summary.errors.extend(f"未知 crawler source：{name}" for name in sorted(unknown))
            return summary
        selected = [
            config
            for config in configs
            if (config.name in requested if requested else config.enabled)
        ]
        for config in selected:
            try:
                self._crawl_source(config, summary)
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"{config.name}: {type(exc).__name__}: {exc}")
        return summary

    def _crawl_source(self, config: CrawlerSourceConfig, summary: CrawlSummary) -> None:
        adapter = FixtureAdapter() if config.adapter == "fixture" else NptuOverviewAdapter()
        if config.adapter == "fixture":
            fixture_path = self._workspace_root / config.url
            listing = fixture_path.read_text(encoding="utf-8")
        elif config.adapter == "nptu_overview":
            listing = self._http.get(config.url)
        else:
            raise ValueError(f"未支援的 adapter：{config.adapter}")
        for candidate in adapter.parse_listing(listing)[: config.max_items]:
            resolved = candidate
            if config.adapter == "fixture":
                detail_path = (self._workspace_root / config.url).with_name("detail.html")
                if detail_path.exists():
                    resolved = self._with_body(candidate, adapter.parse_detail(detail_path.read_text(encoding="utf-8")))
            else:
                try:
                    detail = self._http.get(candidate.canonical_url)
                    resolved = self._with_body(candidate, adapter.parse_detail(detail))
                except Exception:
                    resolved = self._with_warning(candidate, "detail 頁面暫時無法取得，使用 feed 內容")
            result = self._repository.upsert(
                resolved,
                source_name=config.name,
                source_url=(
                    resolved.canonical_url if config.adapter == "fixture" else config.url
                ),
                interval_minutes=config.crawl_interval_minutes,
            )
            setattr(summary, result, getattr(summary, result) + 1)

    @staticmethod
    def _with_body(candidate: AnnouncementCandidate, body: str) -> AnnouncementCandidate:
        return AnnouncementCandidate(
            title=candidate.title,
            canonical_url=candidate.canonical_url,
            unit=candidate.unit,
            category=candidate.category,
            published_at=candidate.published_at,
            deadline_at=candidate.deadline_at,
            body=body or candidate.body,
            warning=candidate.warning,
        )

    @staticmethod
    def _with_warning(candidate: AnnouncementCandidate, warning: str) -> AnnouncementCandidate:
        return AnnouncementCandidate(
            title=candidate.title,
            canonical_url=candidate.canonical_url,
            unit=candidate.unit,
            category=candidate.category,
            published_at=candidate.published_at,
            deadline_at=candidate.deadline_at,
            body=candidate.body,
            warning=warning,
        )
