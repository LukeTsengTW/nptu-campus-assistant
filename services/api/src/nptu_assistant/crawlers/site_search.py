from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import date
import heapq
import time
from typing import Protocol
from urllib.parse import urlsplit

from nptu_assistant.api.schemas import IngestionSummary
from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_source_url
from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePage, NptuSitePageAdapter
from nptu_assistant.crawlers.adapters.nptu_search import AnnouncementSearchResult
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.site_discovery import SiteDiscovery
from nptu_assistant.crawlers.site_models import (
    CandidatePage,
    SearchDiagnostics,
    SearchPlan,
)
from nptu_assistant.crawlers.site_scoring import CandidateScorer, HybridCandidateScorer
from nptu_assistant.ingestion.chunking import TextChunk, chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.providers.protocols import EmbeddingProvider


SITE_SEARCH_PARTIAL_WARNING = "NPTU 網域搜尋有部分頁面無法取得，結果可能不完整。"
SITE_SEARCH_FAILURE_WARNING = (
    "NPTU 網域搜尋目前無法取得頁面，以下內容來自資料庫既有資料。"
)


class SiteSearchHttpClient(Protocol):
    def get(self, url: str, *, allowed_hosts: Collection[str] | None = None) -> str: ...


@dataclass(frozen=True, slots=True)
class SiteSearchResult:
    pages: tuple[NptuSitePage, ...]
    diagnostics: SearchDiagnostics

    @property
    def visited_count(self) -> int:
        return (
            self.diagnostics.fetched_count
            + self.diagnostics.relevant_failure_count
            + self.diagnostics.unrelated_failure_count
        )

    @property
    def failed_count(self) -> int:
        return (
            self.diagnostics.relevant_failure_count
            + self.diagnostics.unrelated_failure_count
        )

    @property
    def query_relevant_failed_count(self) -> int:
        return self.diagnostics.relevant_failure_count


class _ZeroEmbeddingProvider:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _text in texts]


class NptuSiteSearchService:
    def __init__(
        self,
        config: SiteSearchConfig,
        http_client: SiteSearchHttpClient,
        adapter: NptuSitePageAdapter | None = None,
        scorer: CandidateScorer | None = None,
        discovery: SiteDiscovery | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("NPTU 網域搜尋設定尚未啟用")
        self._config = config
        self._http = http_client
        self._adapter = adapter or NptuSitePageAdapter()
        self._scorer = scorer or HybridCandidateScorer(
            config.weights,
            _ZeroEmbeddingProvider(),
        )
        self._discovery = discovery
        self._result_cache: dict[
            tuple[str, tuple[str, ...], tuple[str, ...], int, bool],
            tuple[float, SiteSearchResult],
        ] = {}
        self._page_cache: dict[str, tuple[float, NptuSitePage]] = {}

    @property
    def config(self) -> SiteSearchConfig:
        return self._config

    def search(
        self,
        plan: SearchPlan | str,
        *,
        max_items: int | None = None,
        use_discovery: bool = True,
    ) -> SiteSearchResult:
        requested_limit = self._config.max_items if max_items is None else max_items
        limit = min(requested_limit, self._config.max_items)
        search_plan = (
            SearchPlan.from_query(plan, limit=limit) if isinstance(plan, str) else plan
        )
        if search_plan.limit != limit:
            search_plan = search_plan.model_copy(update={"limit": limit})
        now = time.monotonic()
        cache_key = (*search_plan.cache_key, use_discovery)
        cached_result = self._result_cache.get(cache_key)
        if (
            cached_result is not None
            and self._config.cache_ttl_seconds
            and now - cached_result[0] <= self._config.cache_ttl_seconds
        ):
            return cached_result[1]

        queue: list[tuple[float, int, CandidatePage]] = []
        queued_scores: dict[str, float] = {}
        discovered_urls: set[str] = set()
        visited: set[str] = set()
        pages: list[NptuSitePage] = []
        candidates: list[CandidatePage] = []
        preliminary_scores: list[float] = []
        failed_scores: list[float] = []
        pages_per_host: dict[str, int] = {}
        sequence = 0

        def enqueue(candidate: CandidatePage) -> None:
            nonlocal sequence
            try:
                url = canonicalize_nptu_url(candidate.url)
            except ValueError:
                return
            if (
                url in visited
                or not is_allowed_source_url(url, self._config.allowed_hosts)
                or not self._adapter.is_crawlable_url(url)
            ):
                return
            normalized_candidate = replace(candidate, url=url)
            relevance = self._scorer.score_candidate(search_plan, normalized_candidate)
            current_score = queued_scores.get(url)
            if current_score is not None and current_score >= relevance:
                return
            if (
                url not in discovered_urls
                and len(discovered_urls) >= self._config.max_candidate_urls
            ):
                return
            discovered_urls.add(url)
            queued_scores[url] = relevance
            heapq.heappush(
                queue,
                (-relevance, sequence, normalized_candidate),
            )
            sequence += 1

        if use_discovery and self._discovery is not None:
            try:
                for item in self._discovery.discover(
                    search_plan,
                    max_items=self._config.max_candidate_urls,
                ):
                    enqueue(
                        CandidatePage(
                            item.url,
                            anchor_text=item.label,
                            discovery_relevance=item.relevance,
                        )
                    )
            except Exception:
                pass
        for seed_url in self._config.seed_urls:
            enqueue(CandidatePage(seed_url))

        started_at = time.monotonic()
        while queue and len(visited) < self._config.max_pages:
            if time.monotonic() - started_at >= self._config.query_timeout_seconds:
                failed_scores.extend(-item[0] for item in queue)
                break
            priority, _sequence, candidate = heapq.heappop(queue)
            relevance = -priority
            url = candidate.url
            if relevance < queued_scores.get(url, -1.0):
                continue
            queued_scores.pop(url, None)
            if url in visited:
                continue
            host = urlsplit(url).hostname or ""
            if pages_per_host.get(host, 0) >= self._config.max_pages_per_host:
                continue
            visited.add(url)
            pages_per_host[host] = pages_per_host.get(host, 0) + 1
            try:
                cached_page = self._page_cache.get(url)
                if (
                    cached_page is not None
                    and self._config.cache_ttl_seconds
                    and time.monotonic() - cached_page[0]
                    <= self._config.cache_ttl_seconds
                ):
                    page = cached_page[1]
                else:
                    get_html = getattr(self._http, "get_html", self._http.get)
                    content = get_html(url, allowed_hosts=self._config.allowed_hosts)
                    page = self._adapter.parse_page(
                        content,
                        url,
                        allowed_hosts=self._config.allowed_hosts,
                    )
                    if self._config.cache_ttl_seconds:
                        self._page_cache[url] = (time.monotonic(), page)
            except Exception:
                failed_scores.append(relevance)
                continue

            pages.append(page)
            candidates.append(candidate)
            preliminary = self._scorer.score_candidate(
                search_plan,
                replace(
                    candidate,
                    anchor_text=" ".join(
                        (
                            candidate.anchor_text,
                            page.title,
                            " ".join(page.headings),
                            page.body[:2_000],
                        )
                    ),
                ),
            )
            preliminary_scores.append(preliminary)
            if candidate.depth < self._config.max_depth:
                link_details = page.link_texts or tuple(
                    (link, "") for link in page.links
                )
                for link, label in link_details:
                    enqueue(
                        CandidatePage(
                            link,
                            anchor_text=label,
                            depth=candidate.depth + 1,
                            parent_relevance=preliminary,
                        )
                    )
            if (
                len(pages) >= self._config.early_stop_min_results
                and sum(
                    score >= self._config.high_confidence_score
                    for score in preliminary_scores
                )
                >= self._config.early_stop_min_results
                and (not queue or -queue[0][0] < self._config.relevance_threshold)
            ):
                break

        scores = self._scorer.score_pages(search_plan, candidates, pages)
        scored_pages = [
            replace(page, score=score)
            for page, score in zip(pages, scores, strict=True)
        ]
        relevant_pages = [
            page
            for page in scored_pages
            if page.score >= self._config.relevance_threshold
        ]
        relevant_pages.sort(
            key=lambda page: (
                page.score,
                page.published_at is not None,
                page.published_at or date.min,
            ),
            reverse=True,
        )
        relevant_failed_scores = [
            score
            for score in failed_scores
            if score >= self._config.relevance_threshold
        ]
        unrelated_failed_scores = [
            score for score in failed_scores if score < self._config.relevance_threshold
        ]
        diagnostics = SearchDiagnostics(
            discovered_count=len(discovered_urls),
            fetched_count=len(pages),
            relevant_success_count=len(relevant_pages),
            relevant_failure_count=len(relevant_failed_scores),
            unrelated_failure_count=len(unrelated_failed_scores),
            highest_success_score=max(scores, default=None),
            highest_failed_score=max(relevant_failed_scores, default=None),
        )
        result = SiteSearchResult(tuple(relevant_pages[:limit]), diagnostics)
        if self._config.cache_ttl_seconds:
            self._result_cache[cache_key] = (time.monotonic(), result)
        return result


class DocumentRepository(Protocol):
    def has_hash(self, canonical_url: str, digest: str) -> bool: ...

    def save(
        self,
        metadata: DocumentMetadata,
        raw_text: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
    ) -> None: ...


class ScoredEvidence(Protocol):
    @property
    def score(self) -> float: ...

    @property
    def content(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SitePageIngestionResult:
    summary: IngestionSummary
    warning: str | None
    diagnostics: SearchDiagnostics = SearchDiagnostics()


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

    def should_search_live(self, evidence: Collection[ScoredEvidence]) -> bool:
        reliable = [
            item
            for item in evidence
            if item.score >= self._config.database_min_score
            and len(item.content.strip()) >= self._config.database_min_content_chars
        ]
        return len(reliable) < self._config.database_min_results

    def ingest(
        self,
        plan: SearchPlan | str,
        *,
        max_items: int,
    ) -> SitePageIngestionResult:
        search_result = self._search.search(plan, max_items=max_items)
        summary = IngestionSummary()
        prepared: list[tuple[NptuSitePage, str, str, list[TextChunk]]] = []
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
                prepared.append((page, raw_text, digest, chunks))
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"{page.canonical_url}: {type(exc).__name__}")

        chunk_texts = [
            chunk.content
            for _page, _raw_text, _digest, chunks in prepared
            for chunk in chunks
        ]
        all_embeddings: list[list[float]] = []
        try:
            for start in range(0, len(chunk_texts), self._config.embedding_batch_size):
                all_embeddings.extend(
                    self._embedding_provider.embed(
                        chunk_texts[start : start + self._config.embedding_batch_size]
                    )
                )
        except Exception as exc:
            summary.failed += len(prepared)
            summary.errors.extend(
                f"{page.canonical_url}: {type(exc).__name__}"
                for page, _raw_text, _digest, _chunks in prepared
            )
            return SitePageIngestionResult(
                summary,
                SITE_SEARCH_FAILURE_WARNING,
                search_result.diagnostics,
            )

        embedding_offset = 0
        for page, raw_text, digest, chunks in prepared:
            try:
                embeddings = all_embeddings[
                    embedding_offset : embedding_offset + len(chunks)
                ]
                embedding_offset += len(chunks)
                if len(embeddings) != len(chunks):
                    raise ValueError("頁面分塊與 embedding 數量不一致")
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

        diagnostics = search_result.diagnostics
        warning = self._warning_for(diagnostics)
        if summary.failed:
            warning = (
                SITE_SEARCH_FAILURE_WARNING
                if summary.created == 0 and summary.skipped == 0
                else SITE_SEARCH_PARTIAL_WARNING
            )
        return SitePageIngestionResult(summary, warning, diagnostics)

    def _warning_for(self, diagnostics: SearchDiagnostics) -> str | None:
        if diagnostics.relevant_success_count == 0:
            return (
                SITE_SEARCH_FAILURE_WARNING
                if diagnostics.relevant_failure_count
                else None
            )
        highest_success = diagnostics.highest_success_score or 0.0
        highest_failed = diagnostics.highest_failed_score
        if (
            highest_failed is not None
            and highest_failed > highest_success + self._config.failure_warning_margin
        ):
            return SITE_SEARCH_PARTIAL_WARNING
        return None


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
