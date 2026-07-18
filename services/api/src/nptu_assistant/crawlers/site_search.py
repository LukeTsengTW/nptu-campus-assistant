from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import date
import heapq
import time
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import HttpUrl

from nptu_assistant.api.schemas import IngestionSummary
from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_source_url
from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePage, NptuSitePageAdapter
from nptu_assistant.crawlers.adapters.nptu_search import AnnouncementSearchResult
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.site_discovery import SiteDiscovery
from nptu_assistant.crawlers.site_models import (
    CandidatePage,
    SearchDeadline,
    SearchDeadlineExceeded,
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
    def get(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class SiteSearchResult:
    pages: tuple[NptuSitePage, ...]
    diagnostics: SearchDiagnostics

    @property
    def visited_count(self) -> int:
        return self.diagnostics.visited_count

    @property
    def failed_count(self) -> int:
        return self.diagnostics.failed_count

    @property
    def query_relevant_failed_count(self) -> int:
        return self.diagnostics.query_relevant_failed_count


class _ZeroEmbeddingProvider:
    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        return [[0.0] for _text in texts]


class NptuSiteSearchService:
    def __init__(
        self,
        config: SiteSearchConfig,
        http_client: SiteSearchHttpClient,
        adapter: NptuSitePageAdapter | None = None,
        scorer: CandidateScorer | None = None,
        discovery: SiteDiscovery | None = None,
        clock: Callable[[], float] = time.monotonic,
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
        self._clock = clock
        self._result_cache: dict[
            tuple[str, tuple[str, ...], tuple[str, ...], int, bool],
            tuple[float, SiteSearchResult],
        ] = {}
        self._page_cache: dict[str, tuple[float, NptuSitePage]] = {}

    @property
    def config(self) -> SiteSearchConfig:
        return self._config

    def new_deadline(self) -> SearchDeadline:
        return SearchDeadline.after(
            self._config.query_timeout_seconds,
            clock=self._clock,
        )

    def search(
        self,
        plan: SearchPlan | str,
        *,
        max_items: int | None = None,
        use_discovery: bool = True,
        deadline: SearchDeadline | None = None,
    ) -> SiteSearchResult:
        search_deadline = deadline or self.new_deadline()
        requested_limit = self._config.max_items if max_items is None else max_items
        limit = min(requested_limit, self._config.max_items)
        search_plan = (
            SearchPlan.from_query(plan, limit=limit) if isinstance(plan, str) else plan
        )
        if search_plan.limit != limit:
            search_plan = search_plan.model_copy(update={"limit": limit})
        now = self._clock()
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
        fetch_failure_scores: list[float] = []
        timed_out_scores: list[float] = []
        skipped_candidate_count = 0
        query_timed_out = False
        pages_per_host: dict[str, int] = {}
        sequence = 0

        def enqueue(candidate: CandidatePage) -> None:
            nonlocal sequence, skipped_candidate_count
            try:
                url = canonicalize_nptu_url(candidate.url)
            except ValueError:
                skipped_candidate_count += 1
                return
            if (
                url in visited
                or not is_allowed_source_url(url, self._config.allowed_hosts)
                or not self._adapter.is_crawlable_url(url)
            ):
                skipped_candidate_count += 1
                return
            normalized_candidate = replace(candidate, url=url)
            relevance = self._scorer.score_candidate(search_plan, normalized_candidate)
            current_score = queued_scores.get(url)
            if current_score is not None:
                skipped_candidate_count += 1
                if current_score >= relevance:
                    return
            if (
                url not in discovered_urls
                and len(discovered_urls) >= self._config.max_candidate_urls
            ):
                skipped_candidate_count += 1
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
                search_deadline.raise_if_expired()
                for item in self._discovery.discover(
                    search_plan,
                    max_items=self._config.max_candidate_urls,
                    deadline=search_deadline,
                ):
                    enqueue(
                        CandidatePage(
                            item.url,
                            anchor_text=item.label,
                            discovery_relevance=item.relevance,
                        )
                    )
            except SearchDeadlineExceeded:
                query_timed_out = True
            except Exception:
                pass
        for seed_url in self._config.seed_urls:
            enqueue(CandidatePage(seed_url))

        while queue and len(visited) < self._config.max_pages:
            if search_deadline.expired():
                query_timed_out = True
                timed_out_scores.extend(queued_scores.values())
                break
            priority, _sequence, candidate = heapq.heappop(queue)
            relevance = -priority
            url = candidate.url
            if relevance < queued_scores.get(url, -1.0):
                continue
            queued_scores.pop(url, None)
            if url in visited:
                skipped_candidate_count += 1
                continue
            host = urlsplit(url).hostname or ""
            if pages_per_host.get(host, 0) >= self._config.max_pages_per_host:
                skipped_candidate_count += 1
                continue
            visited.add(url)
            pages_per_host[host] = pages_per_host.get(host, 0) + 1
            try:
                cached_page = self._page_cache.get(url)
                if (
                    cached_page is not None
                    and self._config.cache_ttl_seconds
                    and self._clock() - cached_page[0] <= self._config.cache_ttl_seconds
                ):
                    page = cached_page[1]
                else:
                    get_html = getattr(self._http, "get_html", self._http.get)
                    content = get_html(
                        url,
                        allowed_hosts=self._config.allowed_hosts,
                        timeout_seconds=search_deadline.remaining_seconds(),
                        deadline=search_deadline,
                    )
                    search_deadline.raise_if_expired()
                    page = self._adapter.parse_page(
                        content,
                        url,
                        allowed_hosts=self._config.allowed_hosts,
                    )
                    search_deadline.raise_if_expired()
                    if self._config.cache_ttl_seconds:
                        self._page_cache[url] = (self._clock(), page)
            except SearchDeadlineExceeded:
                query_timed_out = True
                timed_out_scores.append(relevance)
                timed_out_scores.extend(queued_scores.values())
                break
            except Exception:
                fetch_failure_scores.append(relevance)
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

        if queue and not query_timed_out:
            skipped_candidate_count += len(queued_scores)
        try:
            search_deadline.raise_if_expired()
            scores = self._scorer.score_pages(
                search_plan,
                candidates,
                pages,
                deadline=search_deadline,
            )
            search_deadline.raise_if_expired()
        except SearchDeadlineExceeded:
            query_timed_out = True
            scores = preliminary_scores
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
            for score in fetch_failure_scores
            if score >= self._config.relevance_threshold
        ]
        unrelated_failed_scores = [
            score
            for score in fetch_failure_scores
            if score < self._config.relevance_threshold
        ]
        relevant_success_scores = [page.score for page in relevant_pages]
        diagnostics = SearchDiagnostics(
            discovered_count=len(discovered_urls),
            fetched_count=len(pages),
            relevant_success_count=len(relevant_pages),
            relevant_fetch_failure_count=len(relevant_failed_scores),
            unrelated_fetch_failure_count=len(unrelated_failed_scores),
            timed_out_candidate_count=len(timed_out_scores),
            skipped_candidate_count=skipped_candidate_count,
            query_timed_out=query_timed_out,
            highest_success_score=max(relevant_success_scores, default=None),
            highest_fetch_failure_score=max(relevant_failed_scores, default=None),
            highest_unattempted_score=max(timed_out_scores, default=None),
        )
        result = SiteSearchResult(tuple(relevant_pages[:limit]), diagnostics)
        if self._config.cache_ttl_seconds and not query_timed_out:
            self._result_cache[cache_key] = (self._clock(), result)
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
    deadline: SearchDeadline | None = None
    relevant_pages_found: int = 0
    relevant_pages_persisted: int = 0
    ingestion_timed_out: bool = False
    ingestion_complete: bool = True


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

    def new_deadline(self) -> SearchDeadline:
        return self._search.new_deadline()

    def ingest(
        self,
        plan: SearchPlan | str,
        *,
        max_items: int,
        deadline: SearchDeadline | None = None,
    ) -> SitePageIngestionResult:
        deadline = deadline or self.new_deadline()
        search_result = self._search.search(
            plan,
            max_items=max_items,
            deadline=deadline,
        )
        diagnostics = search_result.diagnostics
        summary = IngestionSummary()
        relevant_pages_found = len(search_result.pages)
        relevant_pages_persisted = 0

        def result(
            warning: str | None,
            *,
            timed_out: bool = False,
        ) -> SitePageIngestionResult:
            return SitePageIngestionResult(
                summary=summary,
                warning=warning,
                diagnostics=diagnostics,
                deadline=deadline,
                relevant_pages_found=relevant_pages_found,
                relevant_pages_persisted=relevant_pages_persisted,
                ingestion_timed_out=timed_out,
                ingestion_complete=(
                    relevant_pages_persisted == relevant_pages_found
                    and summary.failed == 0
                ),
            )

        prepared: list[tuple[NptuSitePage, str, str, list[TextChunk]]] = []
        for page in search_result.pages:
            try:
                deadline.raise_if_expired()
                raw_text = page.body.strip()
                if not raw_text:
                    summary.skipped += 1
                    continue
                digest = content_hash(raw_text)
                already_stored = self._repository.has_hash(page.canonical_url, digest)
                deadline.raise_if_expired()
                if already_stored:
                    summary.skipped += 1
                    relevant_pages_persisted += 1
                    continue
                chunks = chunk_text(raw_text)
                deadline.raise_if_expired()
                prepared.append((page, raw_text, digest, chunks))
            except SearchDeadlineExceeded:
                diagnostics = replace(diagnostics, query_timed_out=True)
                return result(
                    self._warning_for(diagnostics),
                    timed_out=True,
                )
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
                deadline.raise_if_expired()
                all_embeddings.extend(
                    self._embedding_provider.embed(
                        chunk_texts[start : start + self._config.embedding_batch_size],
                        timeout_seconds=deadline.remaining_seconds(),
                    )
                )
                deadline.raise_if_expired()
            deadline.raise_if_expired()
        except SearchDeadlineExceeded:
            diagnostics = replace(diagnostics, query_timed_out=True)
            return result(
                self._warning_for(diagnostics),
                timed_out=True,
            )
        except Exception as exc:
            if deadline.expired():
                diagnostics = replace(diagnostics, query_timed_out=True)
                return result(
                    self._warning_for(diagnostics),
                    timed_out=True,
                )
            summary.failed += len(prepared)
            summary.errors.extend(
                f"{page.canonical_url}: {type(exc).__name__}"
                for page, _raw_text, _digest, _chunks in prepared
            )
            return result(SITE_SEARCH_FAILURE_WARNING)

        embedding_offset = 0
        for page, raw_text, digest, chunks in prepared:
            try:
                deadline.raise_if_expired()
                embeddings = all_embeddings[
                    embedding_offset : embedding_offset + len(chunks)
                ]
                embedding_offset += len(chunks)
                if len(embeddings) != len(chunks):
                    raise ValueError("頁面分塊與 embedding 數量不一致")
                metadata = DocumentMetadata(
                    title=page.title,
                    source_url=HttpUrl(page.canonical_url),
                    unit=self._config.unit,
                    published_at=page.published_at,
                    effective_from=page.published_at or date.today(),
                    document_type="official_web_page",
                    version=digest[:12],
                )
                self._repository.save(metadata, raw_text, chunks, embeddings)
                summary.created += 1
                relevant_pages_persisted += 1
                deadline.raise_if_expired()
            except SearchDeadlineExceeded:
                diagnostics = replace(diagnostics, query_timed_out=True)
                break
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"{page.canonical_url}: {type(exc).__name__}")

        warning = self._warning_for(diagnostics)
        if summary.failed:
            warning = (
                SITE_SEARCH_FAILURE_WARNING
                if summary.created == 0 and summary.skipped == 0
                else SITE_SEARCH_PARTIAL_WARNING
            )
        return result(warning, timed_out=diagnostics.query_timed_out)

    def _warning_for(self, diagnostics: SearchDiagnostics) -> str | None:
        if diagnostics.relevant_success_count == 0:
            return (
                SITE_SEARCH_FAILURE_WARNING
                if diagnostics.relevant_fetch_failure_count
                or diagnostics.query_timed_out
                else None
            )
        highest_success = diagnostics.highest_success_score or 0.0
        success_is_sufficient = (
            diagnostics.relevant_success_count >= self._config.early_stop_min_results
            and highest_success >= self._config.high_confidence_score
        )
        if success_is_sufficient:
            return None
        highest_failed = diagnostics.highest_fetch_failure_score
        if (
            highest_failed is not None
            and highest_failed > highest_success + self._config.failure_warning_margin
        ):
            return SITE_SEARCH_PARTIAL_WARNING
        highest_unattempted = diagnostics.highest_unattempted_score
        if (
            diagnostics.query_timed_out
            and highest_unattempted is not None
            and highest_unattempted
            > highest_success + self._config.failure_warning_margin
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
