from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.adapters.factory import build_adapter
from nptu_assistant.crawlers.config import CrawlerSourceConfig, load_source_configs
from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.models import AnnouncementCandidate


class AnnouncementRepository(Protocol):
    def commit_source_refresh(
        self,
        candidates: list[AnnouncementCandidate],
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
        crawled_at: datetime,
    ) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class CrawlRunResult:
    summary: CrawlSummary
    canonical_urls: dict[str, tuple[str, ...]]
    persisted_source_snapshots: frozenset[str] = frozenset()


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
        return self.run_with_urls(source_names).summary

    def run_with_urls(self, source_names: list[str] | None = None) -> CrawlRunResult:
        summary = CrawlSummary()
        canonical_urls: dict[str, tuple[str, ...]] = {}
        persisted_source_snapshots: set[str] = set()
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
            return CrawlRunResult(summary, canonical_urls)
        selected = [
            config
            for config in configs
            if (config.name in requested if requested else config.enabled)
        ]
        for config in selected:
            try:
                canonical_urls[config.name] = self._crawl_source(config, summary)
                persisted_source_snapshots.add(config.name)
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"{config.name}: {type(exc).__name__}: {exc}")
        return CrawlRunResult(
            summary,
            canonical_urls,
            frozenset(persisted_source_snapshots),
        )

    def _crawl_source(
        self,
        config: CrawlerSourceConfig,
        summary: CrawlSummary,
    ) -> tuple[str, ...]:
        adapter = build_adapter(config)
        if config.adapter == "fixture":
            fixture_path = self._workspace_root / config.url
            listing = fixture_path.read_text(encoding="utf-8")
        elif config.adapter == "nptu_html_list":
            if config.dynamic_listing is None:
                listing = self._http.get(config.url, allowed_hosts=config.allowed_hosts)
            else:
                fragment = self._http.submit_form(
                    config.dynamic_listing.method,
                    config.dynamic_listing.url,
                    {},
                    allowed_hosts=config.allowed_hosts,
                )
                listing = (
                    f'<div id="{config.dynamic_listing.wrapper_id}">{fragment}</div>'
                )
        else:
            listing = self._http.get(config.url)
        resolved_candidates: list[AnnouncementCandidate] = []
        for candidate in adapter.parse_listing(listing)[: config.max_items]:
            resolved = candidate
            if config.adapter == "fixture" and self._detail_enabled(config):
                detail_path = (self._workspace_root / config.url).with_name("detail.html")
                if detail_path.exists():
                    resolved = self._with_body(candidate, adapter.parse_detail(detail_path.read_text(encoding="utf-8")))
            elif self._detail_enabled(config):
                try:
                    detail = (
                        self._http.get(
                            candidate.canonical_url,
                            allowed_hosts=config.allowed_hosts,
                        )
                        if config.adapter == "nptu_html_list"
                        else self._http.get(candidate.canonical_url)
                    )
                    resolved = self._with_body(candidate, adapter.parse_detail(detail))
                except Exception:
                    resolved = self._with_warning(candidate, "公告詳情暫時無法取得，使用列表內容")
            resolved_candidates.append(resolved)

        source_url = (
            resolved_candidates[0].canonical_url
            if config.adapter == "fixture" and resolved_candidates
            else config.url
        )
        results = self._repository.commit_source_refresh(
            resolved_candidates,
            source_name=config.name,
            source_url=source_url,
            source_unit=config.unit,
            interval_minutes=config.crawl_interval_minutes,
            crawled_at=datetime.now(timezone.utc),
        )
        if len(results) != len(resolved_candidates):
            raise RuntimeError("announcement repository 回傳的批次結果數量不一致")
        canonical_urls: list[str] = []
        for resolved, result in zip(resolved_candidates, results, strict=True):
            setattr(summary, result, getattr(summary, result) + 1)
            canonical_urls.append(resolved.canonical_url)
        return tuple(dict.fromkeys(canonical_urls))

    @staticmethod
    def _detail_enabled(config: CrawlerSourceConfig) -> bool:
        if config.detail is not None:
            return config.detail.enabled
        return config.adapter in {"fixture", "nptu_overview"}

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
