from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.aliases import AliasNormalizer
from nptu_assistant.crawlers.adapters.nptu_search import (
    AnnouncementSearchResult,
    NptuAssociationSearchAdapter,
    SearchForm,
)
from nptu_assistant.crawlers.config import KeywordSearchConfig
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    site_page_to_announcement_result,
)


PARTIAL_SEARCH_FAILURE_WARNING = "部分官網公告搜尋失敗，以下結果可能不完整。"
FULL_SEARCH_FAILURE_WARNING = "本次官網公告搜尋失敗，以下內容來自資料庫最後成功收錄的資料。"


@dataclass(frozen=True, slots=True)
class KeywordExpansion:
    search_terms: tuple[str, ...]
    retrieval_query: str


@dataclass(frozen=True, slots=True)
class KeywordIngestionResult:
    retrieval_query: str
    summary: CrawlSummary
    warning: str | None = None
    canonical_urls: tuple[str, ...] | None = None


class KeywordAliasResolver(AliasNormalizer):
    def expand(self, query: str) -> KeywordExpansion:
        query = query.strip()
        if not query:
            raise ValueError("公告搜尋關鍵字不得為空")
        normalized = self.normalize(query)
        terms = tuple(dict.fromkeys((query, normalized)))
        return KeywordExpansion(terms, normalized)


class AnnouncementRepository(Protocol):
    def upsert(
        self,
        candidate: AnnouncementCandidate,
        *,
        source_name: str,
        source_url: str,
        interval_minutes: int,
    ) -> str: ...


class SearchHttpClient(Protocol):
    def get(self, url: str) -> str: ...

    def submit_form(self, method: str, url: str, fields: Mapping[str, str]) -> str: ...


class KeywordAnnouncementSearchService:
    def __init__(
        self,
        config: KeywordSearchConfig,
        repository: AnnouncementRepository,
        http_client: SearchHttpClient,
        adapter: NptuAssociationSearchAdapter | None = None,
        site_searcher: NptuSiteSearchService | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._http = http_client
        self._adapter = adapter or NptuAssociationSearchAdapter()
        self._resolver = KeywordAliasResolver(config.aliases)
        self._site_searcher = site_searcher

    def normalize(self, text: str) -> str:
        return self._resolver.normalize(text)

    def _load_form(self) -> SearchForm:
        self._http.get(self._config.session_url)
        bootstrap_content = self._http.submit_form(
            self._config.bootstrap_method,
            self._config.bootstrap_url,
            {},
        )
        bootstrap = self._adapter.parse_bootstrap_form(
            bootstrap_content,
            self._config.bootstrap_url,
        )
        return SearchForm(
            bootstrap.method,
            self._config.url,
            bootstrap.hidden_fields,
            tuple(self._config.search_types),
        )

    def ingest(self, query: str, *, max_items: int | None = None) -> KeywordIngestionResult:
        expansion = self._resolver.expand(query)
        summary = CrawlSummary()
        item_limit = self._config.max_items if max_items is None else min(max_items, self._config.max_items)
        form: SearchForm | None = None
        successful_searches = 0
        try:
            form = self._load_form()
        except Exception as exc:
            summary.failed = 1
            summary.errors.append(f"官方搜尋表單無法載入：{type(exc).__name__}")

        results: list[AnnouncementSearchResult] = []
        if form is not None:
            for search_term in expansion.search_terms:
                for search_type in self._config.search_types:
                    last_error: Exception | None = None
                    for _attempt in range(2):
                        try:
                            if form is None:
                                form = self._load_form()
                            if search_type not in form.search_types:
                                raise ValueError(f"官網搜尋表單缺少分類：{search_type}")
                            fields = dict(form.hidden_fields)
                            fields.update({"SchKey": search_term, "SchType": search_type})
                            content = self._http.submit_form(form.method, form.action_url, fields)
                            parsed_results = self._adapter.parse_results(content, form.action_url)
                            next_form = self._adapter.parse_form(content, self._config.url)
                            results.extend(parsed_results)
                            form = next_form
                            successful_searches += 1
                            break
                        except Exception as exc:
                            last_error = exc
                            form = None
                    else:
                        summary.failed += 1
                        error_name = type(last_error).__name__ if last_error else "RuntimeError"
                        summary.errors.append(f"{search_type} 搜尋失敗：{error_name}")

        if self._site_searcher is not None:
            try:
                site_result = self._site_searcher.search(
                    expansion.retrieval_query,
                    max_items=item_limit,
                )
                results.extend(
                    site_page_to_announcement_result(page, config=self._site_searcher.config)
                    for page in site_result.pages
                    if page.published_at is not None
                )
                if site_result.visited_count > site_result.failed_count:
                    successful_searches += 1
                if site_result.failed_count:
                    summary.failed += site_result.failed_count
                    summary.errors.append(f"NPTU 網域頁面搜尋失敗：{site_result.failed_count} 個頁面")
            except Exception:
                summary.failed += 1
                summary.errors.append("NPTU 網域搜尋失敗：RuntimeError")

        unique_results: dict[str, AnnouncementSearchResult] = {}
        for result in results:
            unique_results.setdefault(result.canonical_url, result)
        ordered = sorted(
            unique_results.values(),
            key=lambda item: (item.published_at is not None, item.published_at or date.min),
            reverse=True,
        )[:item_limit]

        ingested_urls: list[str] = []
        for result in ordered:
            if result.published_at is None:
                summary.failed += 1
                summary.errors.append(f"公告缺少發布日期：{result.title}")
                continue
            warning: str | None = None
            body = result.body
            try:
                body = self._adapter.parse_detail(self._http.get(result.canonical_url))
            except Exception:
                warning = "公告詳情暫時無法取得，已保留搜尋結果摘要。"
            candidate = AnnouncementCandidate(
                title=result.title,
                canonical_url=result.canonical_url,
                unit=result.unit or self._config.unit,
                category=result.category or self._config.category,
                published_at=result.published_at,
                deadline_at=None,
                body=body,
                warning=warning,
            )
            try:
                outcome = self._repository.upsert(
                    candidate,
                    source_name=result.source_name or self._config.name,
                    source_url=result.source_url or self._config.url,
                    interval_minutes=self._config.crawl_interval_minutes,
                )
                ingested_urls.append(result.canonical_url)
                if outcome == "created":
                    summary.created += 1
                elif outcome == "updated":
                    summary.updated += 1
                else:
                    summary.unchanged += 1
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"公告收錄失敗：{type(exc).__name__}")

        if successful_searches == 0:
            warning = FULL_SEARCH_FAILURE_WARNING
        elif summary.failed:
            warning = PARTIAL_SEARCH_FAILURE_WARNING
        else:
            warning = None
        return KeywordIngestionResult(
            expansion.retrieval_query,
            summary,
            warning,
            tuple(ingested_urls) if successful_searches else None,
        )
