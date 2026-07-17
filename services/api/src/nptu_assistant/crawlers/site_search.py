from __future__ import annotations

from collections import deque
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
import re
from typing import Protocol

from nptu_assistant.api.schemas import IngestionSummary
from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_source_url
from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePage, NptuSitePageAdapter
from nptu_assistant.crawlers.adapters.nptu_search import AnnouncementSearchResult
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.ingestion.chunking import TextChunk, chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.providers.protocols import EmbeddingProvider


SITE_SEARCH_PARTIAL_WARNING = "NPTU 網域搜尋有部分頁面無法取得，結果可能不完整。"
SITE_SEARCH_FAILURE_WARNING = "NPTU 網域搜尋目前無法取得頁面，以下內容來自資料庫既有資料。"


class SiteSearchHttpClient(Protocol):
    def get(self, url: str, *, allowed_hosts: Collection[str] | None = None) -> str: ...


@dataclass(frozen=True, slots=True)
class SiteSearchResult:
    pages: tuple[NptuSitePage, ...]
    visited_count: int
    failed_count: int


class NptuSiteSearchService:
    def __init__(
        self,
        config: SiteSearchConfig,
        http_client: SiteSearchHttpClient,
        adapter: NptuSitePageAdapter | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("NPTU 網域搜尋設定尚未啟用")
        self._config = config
        self._http = http_client
        self._adapter = adapter or NptuSitePageAdapter()

    @property
    def config(self) -> SiteSearchConfig:
        return self._config

    def search(self, query: str, *, max_items: int | None = None) -> SiteSearchResult:
        normalized_query = " ".join(query.split()).casefold()
        if not normalized_query:
            raise ValueError("網站搜尋關鍵字不得為空")
        terms = tuple(term for term in re.split(r"\s+", normalized_query) if term)
        limit = self._config.max_items if max_items is None else min(max_items, self._config.max_items)
        queue = deque(self._config.seed_urls)
        queued = set(queue)
        visited: set[str] = set()
        matches: list[NptuSitePage] = []
        failed_count = 0

        while queue and len(visited) < self._config.max_pages:
            url = queue.popleft()
            queued.discard(url)
            try:
                url = canonicalize_nptu_url(url)
            except ValueError:
                failed_count += 1
                continue
            if url in visited or not is_allowed_source_url(url, self._config.allowed_hosts):
                continue
            visited.add(url)
            try:
                content = self._http.get(url, allowed_hosts=self._config.allowed_hosts)
                page = self._adapter.parse_page(
                    content,
                    url,
                    allowed_hosts=self._config.allowed_hosts,
                )
            except Exception:
                failed_count += 1
                continue

            searchable = f"{page.title}\n{page.body}".casefold()
            if all(term in searchable for term in terms):
                matches.append(page)
            for link in page.links:
                if link not in visited and link not in queued:
                    queue.append(link)
                    queued.add(link)

        matches.sort(
            key=lambda page: (page.published_at is not None, page.published_at or date.min),
            reverse=True,
        )
        return SiteSearchResult(tuple(matches[:limit]), len(visited), failed_count)


class DocumentRepository(Protocol):
    def has_hash(self, canonical_url: str, digest: str) -> bool: ...

    def save(
        self,
        metadata: DocumentMetadata,
        raw_text: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SitePageIngestionResult:
    summary: IngestionSummary
    warning: str | None


class SitePageIngestionService:
    def __init__(
        self,
        search_service: NptuSiteSearchService,
        repository: DocumentRepository,
        embedding_provider: EmbeddingProvider,
        config: SiteSearchConfig,
    ) -> None:
        self._search = search_service
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._config = config

    def ingest(self, query: str, *, max_items: int) -> SitePageIngestionResult:
        search_result = self._search.search(query, max_items=max_items)
        summary = IngestionSummary()
        for page in search_result.pages:
            try:
                raw_text = page.body.strip()
                if not raw_text:
                    summary.skipped += 1
                    continue
                digest = content_hash(raw_text)
                if self._repository.has_hash(page.canonical_url, digest):
                    summary.skipped += 1
                    continue
                chunks = chunk_text(raw_text)
                embeddings = self._embedding_provider.embed([chunk.content for chunk in chunks])
                metadata = DocumentMetadata(
                    title=page.title,
                    source_url=page.canonical_url,
                    unit=self._config.unit,
                    published_at=page.published_at,
                    effective_from=page.published_at or date.today(),
                    document_type="official_web_page",
                    version=digest[:12],
                )
                self._repository.save(metadata, raw_text, chunks, embeddings)
                summary.created += 1
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"{page.canonical_url}: {type(exc).__name__}")

        warning = None
        if search_result.failed_count:
            warning = (
                SITE_SEARCH_FAILURE_WARNING
                if not search_result.visited_count - search_result.failed_count
                else SITE_SEARCH_PARTIAL_WARNING
            )
        return SitePageIngestionResult(summary, warning)


def site_page_to_announcement_result(
    page: NptuSitePage,
    *,
    config: SiteSearchConfig,
) -> AnnouncementSearchResult:
    return AnnouncementSearchResult(
        title=page.title,
        canonical_url=page.canonical_url,
        unit=config.unit,
        category=config.category,
        published_at=page.published_at,
        body=page.body,
        source_name=config.name,
        source_url=config.seed_urls[0],
    )
